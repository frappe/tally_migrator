from dataclasses import dataclass
from typing import Callable

import frappe

from tally_migrator.tally.config import TallyConfig
from tally_migrator.tally.extractors import TallyExtractor, ExtractedMasters
from tally_migrator.erpnext.importers import ERPNextImporter, ImportResult
from tally_migrator.migration.overrides import apply_record_overrides


def _has_opening(raw) -> bool:
    """True when a Tally opening-balance cell carries a non-zero amount.

    Handles Dr/Cr suffixes, multi-currency cells and thousands separators by
    reusing the extractor's own parser, so the pipeline-gating check agrees
    exactly with what the importer will post.
    """
    return TallyExtractor._parse_opening(raw)[0] != 0.0


@dataclass
class PipelineStep:
    """One entity in the migration pipeline: how to import it and how to report it."""
    label: str                                   # e.g. "Warehouses"
    percent: int                                 # progress-bar position
    importer: Callable[[list[dict]], ImportResult]
    records: list[dict]


class MigrationSummary:
    """Per-entity import results, keyed by label in pipeline order.

    Holding the results in one ordered mapping (rather than a fixed field per
    entity) means adding a new entity to the pipeline needs no change here - the
    summary, the log, and the error reporting all iterate generically.
    """

    def __init__(self, results: dict[str, ImportResult]):
        self.results = results

    def as_dict(self) -> dict:
        return {label: result.as_dict() for label, result in self.results.items()}

    @property
    def has_errors(self) -> bool:
        return any(result.failed > 0 for result in self.results.values())

    @property
    def has_warnings(self) -> bool:
        return any(result.warned > 0 for result in self.results.values())

    def error_lines(self) -> str:
        """Flat, human-readable list of per-record failures + non-fatal drops."""
        lines = [
            f"[{label}] {e['name']}: {e['reason']}"
            for label, result in self.results.items()
            for e in result.errors
        ]
        warns = [
            f"[{label}] ⚠ {w['name']}: {w['reason']}"
            for label, result in self.results.items()
            for w in result.warnings
        ]
        if warns:
            lines += ["", "Warnings (non-fatal - record imported, dependent data dropped):"] + warns
        return "\n".join(lines)

    def created_records(self) -> dict:
        """Per-entity list of the ERPNext doc names actually inserted this run.

        This is the authoritative 'what did this migration touch' record - it
        includes the opening Journal Entry and Stock Reconciliation names, so the
        run can be reviewed or reversed by inspection. Empty entities are omitted."""
        return {
            label: result.created_names
            for label, result in self.results.items()
            if result.created_names
        }

    def error_records(self) -> list[dict]:
        """Structured per-record failures + non-fatal drops for the log's table.

        Both land in the log's issues table so nothing is lost silently; warnings
        are prefixed so they're distinguishable from hard failures and do not flip
        the run status to 'Completed with Errors'."""
        rows = [
            {"record_type": label, "record_name": e["name"], "reason": e["reason"]}
            for label, result in self.results.items()
            for e in result.errors
        ]
        rows += [
            {"record_type": f"{label} (warning)", "record_name": w["name"],
             "reason": f"⚠ {w['reason']}"}
            for label, result in self.results.items()
            for w in result.warnings
        ]
        return rows


class MasterMigrator:
    """
    Phase 1 orchestrator: Tally Masters → ERPNext.

    Order of operations
    -------------------
    Warehouses first  - Items reference warehouses.
    Customers / Suppliers next - independent of each other.
    Items last - depend on Item Groups and Warehouses.

    A ``Tally Migration Log`` is created with status ``Running`` *before* any
    work starts and finalized at the end, so an interrupted run still leaves an
    auditable record. Progress is published to the Frappe realtime bus for an
    optional live progress bar (best-effort; the call also returns the summary).

    Scaling note: for very large datasets, ``run`` is a natural unit to move into
    a background job (``frappe.enqueue``) keyed off the log document. It is kept
    synchronous here for reliability - the summary is returned directly rather
    than depending on the realtime channel.
    """

    STEPS = {
        0: "Reading uploaded file...",
        10: "Extracting masters from file...",
        25: "Importing Warehouses...",
        45: "Importing Customers...",
        60: "Importing Suppliers...",
        75: "Importing Items...",
        95: "Saving migration log...",
        100: "Migration complete.",
    }

    def __init__(self, config: TallyConfig, source, uom_overrides: dict | None = None,
                 record_overrides: dict | None = None, log=None):
        """``source`` is any object exposing ``ping()`` + ``get_collection``.

        In practice this is a :class:`FileTallySource` wrapping an uploaded
        Tally masters XML export.

        ``record_overrides`` are per-record field fixes from the pre-flight screen,
        applied to the extracted records in memory before import.

        ``log`` lets a caller create the ``Tally Migration Log`` up front (e.g. so a
        background-job dispatcher can return its name immediately) and have this run
        reuse it instead of creating a new one. ``source`` may be ``None`` when the
        instance is built only to create the log.
        """
        self.config = config
        self.client = source
        self.extractor = TallyExtractor(self.client)
        self.uom_overrides = uom_overrides or {}
        self.importer = ERPNextImporter(
            config.erpnext_company,
            uom_overrides=self.uom_overrides,
            coa_mode=getattr(config, "coa_mode", "reuse"),
        )
        self.record_overrides = record_overrides or {}
        self.applied_edits: list[dict] = []   # audit trail of effective pre-flight edits
        self.posting_date = getattr(config, "posting_date", "") or ""
        self.log = log

    # ── Public ────────────────────────────────────────────────────────────────

    def run(self) -> MigrationSummary:
        # Reuse a log handed in by the dispatcher (background runs); otherwise
        # create one now so an interrupted run is still recorded.
        self.log = self.log or self._create_log()
        try:
            self._progress(0)
            if not self.client.ping():
                frappe.throw("Could not read the uploaded file. Please re-upload a valid Tally Masters XML export.")

            self._progress(10)
            masters = self.extractor.extract_all()
            apply_record_overrides(masters, self.record_overrides, self.applied_edits)
            coa = self.extractor.extract_coa()
            # Bill-wise party opening detail (BILLALLOCATIONS) - empty when the
            # source can't supply child lists, so party openings then degrade to a
            # single lump opening invoice per party (no bill breakdown).
            bills = self.extractor.extract_bill_allocations()
            frappe.logger().info(
                f"[Tally Migrator] Extracted: {masters.summary} | COA: {coa.summary} "
                f"| bills: {len(bills)}")

            results: dict[str, ImportResult] = {}
            for step in self._pipeline(masters, coa, bills):
                self._progress(step.percent, f"Importing {len(step.records)} {step.label.lower()}...")
                results[step.label] = step.importer(step.records)
            self._record_excluded(results, coa)
            summary = MigrationSummary(results)

            self._progress(95)
            self._finalize_log(masters, coa, summary)

            self._progress(100)
            return summary
        except Exception as exc:
            self._fail_log(exc)
            raise

    def _pipeline(self, masters: ExtractedMasters, coa, bills) -> list[PipelineStep]:
        """Entity import order. Adding an entity = add one step here.

        Accounts (COA) first - opening balances post against them; Cost Centres
        next; then Warehouses (Items reference them), Customers/Suppliers
        (independent), Items (depend on Item Groups + Warehouses); Opening
        Balances last, once every account exists.
        """
        steps: list[PipelineStep] = []
        if coa.accounts:
            steps.append(PipelineStep("Accounts", 20, self.importer.import_accounts, coa.accounts))
        if coa.cost_centres:
            steps.append(PipelineStep("Cost Centres", 30, self.importer.import_cost_centres, coa.cost_centres))
        steps.append(
            PipelineStep("Warehouses", 40, self.importer.import_warehouses, masters.warehouses))
        # Inventory structure masters before Items - an item references its group
        # and UOM, so create the nested Item Groups and UOMs first.
        if masters.units:
            steps.append(PipelineStep("Units", 44, self.importer.import_units, masters.units))
        if masters.stock_groups:
            steps.append(PipelineStep(
                "Stock Groups", 48, self.importer.import_stock_groups, masters.stock_groups))
        steps += [
            PipelineStep("Customers", 55, self.importer.import_customers, masters.customers),
            PipelineStep("Suppliers", 65, self.importer.import_suppliers, masters.suppliers),
            PipelineStep("Items", 80, self.importer.import_items, masters.items),
        ]
        # Ledger account opening balances (cash, assets, P&L) - one balanced,
        # submitted Opening Entry (JE) once every account exists. Party balances
        # NO LONGER go through this path: they post invoice-wise below, so the JE
        # gets empty customer/supplier lists and covers ledger accounts only.
        ledger_ob = any(a.opening_balance and not a.is_group for a in coa.accounts)
        if ledger_ob:
            steps.append(PipelineStep(
                "Opening Balances", 90,
                lambda _records: self.importer.import_opening_balances(
                    coa.accounts, [], [], self.posting_date),
                coa.accounts,
            ))
        # Party (Customer/Supplier) opening balances - invoice-wise: one opening
        # Sales/Purchase Invoice per outstanding bill, a Payment Entry per advance,
        # posted once every party exists. Gated on either bill-wise detail or a
        # party ledger opening (a party with an opening but no bills falls back to
        # a single lump opening invoice). See PartyOpeningImporter.
        party_ob = any(_has_opening(r.get("OpeningBalance"))
                       for r in (*masters.customers, *masters.suppliers))
        if bills or party_ob:
            steps.append(PipelineStep(
                "Party Openings", 91,
                lambda _records, c=masters.customers, s=masters.suppliers, b=bills:
                    self.importer.import_party_openings(b, c, s, self.posting_date),
                masters.customers,
            ))
        # Opening stock: item opening quantities → one submitted Stock Reconciliation.
        # Item opening balances are unit-suffixed quantities ("55 Nos"), so gate on
        # the quantity parser, not the amount parser (which reads them as zero).
        if any(TallyExtractor._parse_quantity(i.get("OpeningBalance")) != 0
               for i in masters.items):
            steps.append(PipelineStep(
                "Opening Stock", 93,
                lambda items: self.importer.import_opening_stock(items, self.posting_date),
                masters.items))
        return steps

    def _record_excluded(self, results: dict, coa) -> None:
        """Fold COA-excluded ledgers into the summary as non-fatal warnings, so a
        ledger that was intentionally (or unexpectedly) left out of the Chart of
        Accounts is visible in the migration log instead of vanishing silently."""
        excluded = getattr(coa, "excluded", None)
        if not excluded:
            return
        acc = results.get("Accounts") or ImportResult("Account")
        for ex in excluded:
            acc.add_warning(ex["name"], ex["reason"])
        results["Accounts"] = acc

    # ── Progress ────────────────────────────────────────────────────────────────

    def _progress(self, pct: int, description: str = "") -> None:
        frappe.publish_progress(
            pct,
            title="Tally Masters Migration",
            description=description or self.STEPS.get(pct, ""),
        )

    # ── Migration log lifecycle ──────────────────────────────────────────────────

    def _create_log(self):
        """Insert a 'Running' log up front so an interrupted run is still recorded."""
        log = frappe.new_doc("Tally Migration Log")
        log.company = self.config.erpnext_company
        log.tally_company = self.config.tally_company
        log.migration_type = "Masters"
        log.status = "Running"
        if self.config.source_file:
            log.source_file = self.config.source_file
        if self.config.validation_report:
            log.validation_report = self.config.validation_report
        if getattr(self.config, "coverage_report", ""):
            log.coverage_report = self.config.coverage_report
        if getattr(self.config, "mapping_report", ""):
            log.mapping_report = self.config.mapping_report
        # Persisted so a re-run from this log repeats the original options faithfully.
        log.coa_mode = getattr(self.config, "coa_mode", "reuse") or "reuse"
        if self.posting_date:
            log.posting_date = self.posting_date
        # The user's pre-flight UOM mappings and per-record fixes - kept so a re-run
        # reproduces the migration that was validated, not the raw/default data.
        if self.uom_overrides:
            log.uom_overrides = frappe.as_json(self.uom_overrides)
        if self.record_overrides:
            log.record_overrides = frappe.as_json(self.record_overrides)
        log.insert(ignore_permissions=True)
        frappe.db.commit()
        return log

    def _finalize_log(self, masters: ExtractedMasters, coa, summary: MigrationSummary) -> None:
        """Record extraction/import results. Must never abort the migration."""
        try:
            self.log.reload()
            self.log.status = "Completed with Errors" if summary.has_errors else "Completed"
            self.log.extracted_counts = frappe.as_json({**masters.summary, **coa.summary})
            self.log.import_summary = frappe.as_json(summary.as_dict())
            self.log.applied_edits = frappe.as_json(self.applied_edits)
            self.log.created_records = frappe.as_json(summary.created_records())
            self.log.set("errors", [])
            if summary.has_errors or summary.has_warnings:
                self.log.error_log = summary.error_lines()
                for row in summary.error_records():
                    self.log.append("errors", row)
            self.log.save(ignore_permissions=True)
            frappe.db.commit()
        except Exception as exc:
            frappe.log_error(f"Migration log finalize failed: {exc}", "Tally Migrator")

    def _fail_log(self, exc: Exception) -> None:
        """Mark the log 'Failed' with a traceback. Best-effort; never re-raises."""
        try:
            frappe.db.rollback()
            if self.log:
                self.log.reload()
                self.log.status = "Failed"
                self.log.error_log = frappe.get_traceback() or str(exc)
                self.log.save(ignore_permissions=True)
                frappe.db.commit()
        except Exception as inner:
            frappe.log_error(f"Migration log fail-update failed: {inner}", "Tally Migrator")
