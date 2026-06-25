from dataclasses import dataclass
from typing import Callable

import frappe

from tally_migrator.tally.config import TallyConfig
from tally_migrator.tally.extractors import TallyExtractor, ExtractedMasters
from tally_migrator.erpnext.importers import ERPNextImporter, ImportResult
from tally_migrator.migration.overrides import apply_record_overrides, uom_edits


def _collapse_identical(rows: list[dict], sample: int = 5) -> list[dict]:
    """Merge issue rows that share the exact same (record_type, reason).

    A systemic problem hits many records with byte-identical messages (no HSN,
    zero-rate opening stock, the same dependent-doc drop). Collapsing them keeps
    the log's issues table readable: one row per distinct issue, whose Record
    column lists the affected records (a short sample + an "(+N more)" tail) and
    whose reason is prefixed with the count. Rows whose reason embeds the record
    name are naturally unique and pass through unchanged.

    Order is preserved by first appearance, so the table still reads top-to-bottom
    in pipeline order.
    """
    groups: dict[tuple, dict] = {}
    order: list[tuple] = []
    for r in rows:
        # Status is part of the key so a hard failure and a non-fatal skip that happen
        # to share a record_type + reason are never merged into one row.
        key = (r["status"], r["record_type"], r["reason"])
        if key not in groups:
            groups[key] = {"status": r["status"], "record_type": r["record_type"],
                           "names": [], "reason": r["reason"]}
            order.append(key)
        groups[key]["names"].append(str(r["record_name"]))

    out: list[dict] = []
    for key in order:
        g = groups[key]
        names, n = g["names"], len(g["names"])
        if n == 1:
            out.append({"status": g["status"], "record_type": g["record_type"],
                        "record_name": names[0], "reason": g["reason"]})
            continue
        shown = ", ".join(names[:sample])
        more = f" (+{n - sample} more)" if n > sample else ""
        # Prepend the count so the table headline reads e.g. "13 records · opening
        # stock …". The Status column (Failed / Skipped) tells the two apart.
        out.append({
            "status": g["status"],
            "record_type": g["record_type"],
            "record_name": f"{shown}{more}",
            "reason": f"{n} records · {g['reason']}",
        })
    return out


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
            f"[{label}] {w['name']}: {w['reason']}"
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
        run can be reviewed or reversed by inspection. Empty entities are omitted.

        Each entry is ``{name, doctype}`` (plus an optional ``label`` when the
        doc's ``name`` is an opaque autoname, e.g. an Item Price) so the log can
        deep-link it, since one importer can create several doctypes (party
        openings -> Sales/Purchase Invoice + Payment Entry; an account -> its
        Bank Account)."""
        return {
            label: result.created_docs
            for label, result in self.results.items()
            if result.created_docs
        }

    def error_records(self) -> list[dict]:
        """Structured per-record failures + non-fatal drops for the log's table.

        Both land in the log's issues table so nothing is lost silently; warnings
        are prefixed so they're distinguishable from hard failures and do not flip
        the run status to 'Completed with Errors'.

        Rows that share the *exact* same (record_type, reason) are collapsed into a
        single row so one systemic issue (e.g. dozens of items with no HSN, or
        opening stock posted at a zero rate) reads as one line listing the affected
        records, instead of flooding the table with identical messages."""
        rows = [
            {"status": "Failed", "record_type": label,
             "record_name": e["name"], "reason": e["reason"]}
            for label, result in self.results.items()
            for e in result.errors
        ]
        rows += [
            {"status": "Skipped", "record_type": label,
             "record_name": w["name"], "reason": w["reason"]}
            for label, result in self.results.items()
            for w in result.warnings
        ]
        return _collapse_identical(rows)


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
                 record_overrides: dict | None = None, log=None,
                 created_uoms: list | None = None):
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
        # UOMs the pre-flight "create as new" step (create_uoms) inserted BEFORE this
        # run. The unit importer then skips them as already-existing and never records
        # them, so they would survive a revert. We fold them into the manifest at
        # finalize so revert undoes them too. See _track_wizard_uoms.
        self.created_uoms = created_uoms or []
        self.applied_edits: list[dict] = []   # audit trail of effective pre-flight edits
        self.posting_date = getattr(config, "posting_date", "") or ""
        self.log = log
        # Wall-time (seconds) per phase, for the performance baseline / before-after
        # comparison. Folded into the log's extracted_counts under "_phase_seconds" at
        # finalize and logged. Pure measurement - no effect on what gets imported.
        self._timings: dict[str, float] = {}

    # ── Public ────────────────────────────────────────────────────────────────

    def run(self) -> MigrationSummary:
        # Reuse a log handed in by the dispatcher (background runs); otherwise
        # create one now so an interrupted run is still recorded.
        self.log = self.log or self._create_log()
        try:
            import time as _time
            self._progress(0)
            if not self.client.ping():
                frappe.throw("Could not read the uploaded file. Please re-upload a valid Tally Masters XML export.")

            self._progress(10)
            _t = _time.monotonic()
            masters = self.extractor.extract_all()
            apply_record_overrides(masters, self.record_overrides, self.applied_edits)
            self._record_uom_edits()
            coa = self.extractor.extract_coa()
            # Bill-wise party opening detail (BILLALLOCATIONS) - empty when the
            # source can't supply child lists, so party openings then degrade to a
            # single lump opening invoice per party (no bill breakdown).
            bills = self.extractor.extract_bill_allocations()
            self._timings["Extract"] = round(_time.monotonic() - _t, 2)
            frappe.logger().info(
                f"[Tally Migrator] Extracted: {masters.summary} | COA: {coa.summary} "
                f"| bills: {len(bills)} | extract {self._timings['Extract']}s")

            results: dict[str, ImportResult] = {}
            steps = self._pipeline(masters, coa, bills)
            for i, step in enumerate(steps):
                self._progress(step.percent, f"Importing {len(step.records)} {step.label.lower()}...")
                # The bar fills from this step's percent up to (but not reaching) the
                # next step's percent, so a long phase keeps moving instead of sitting
                # frozen. The finalize milestone (95) caps the last step's band.
                band_end = steps[i + 1].percent if i + 1 < len(steps) else 95
                self.importer.on_progress = self._step_progress_cb(
                    step.label, step.percent, band_end)
                _t = _time.monotonic()
                try:
                    results[step.label] = step.importer(step.records)
                finally:
                    self.importer.on_progress = None
                self._timings[step.label] = round(_time.monotonic() - _t, 2)
                frappe.logger().info(
                    f"[Tally Migrator] Phase '{step.label}': {len(step.records)} records "
                    f"in {self._timings[step.label]}s")
            self._record_excluded(results, coa)
            summary = MigrationSummary(results)

            self._progress(95)
            self._finalize_log(masters, coa, summary)

            self._progress(100)
            frappe.logger().info(f"[Tally Migrator] Phase timings (s): {self._timings}")
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
        # Batch masters (Tally batch-wise opening detail) -> ERPNext Batch. After
        # Items (the item must carry has_batch_no) and before Opening Stock (the
        # reconciliation posts batch-wise rows against these). Gated on any item
        # carrying a batch in its godown-wise opening detail.
        if any((i.get("IsBatchWiseOn") or "").strip().lower() == "yes" for i in masters.items):
            steps.append(PipelineStep("Batches", 81, self.importer.import_batches, masters.items))
        # Price levels (Retail/Wholesale) -> Price List + Item Price (+ discount
        # Pricing Rule); also Tally MRP -> an "MRP" Price List + Item Price. After
        # Items so item_code/UOM links resolve.
        if any(i.get("PriceLevels") or i.get("Mrp") for i in masters.items):
            steps.append(PipelineStep("Prices", 82, self.importer.import_prices, masters.items))
        # Bills of materials -> ERPNext BOM (submitted). After Items so the finished
        # item and every component exist.
        if any(i.get("Boms") for i in masters.items):
            steps.append(PipelineStep("BOMs", 84, self.importer.import_boms, masters.items))
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

    # Emit an intra-phase progress update at most every this many records (or
    # whenever the whole-number percent ticks up). Bounds the realtime/Redis writes
    # to roughly a hundred over a full run - cheap enough to never dent throughput -
    # while still moving the counter often enough to read as continuous.
    _PROGRESS_EVERY = 250

    def _step_progress_cb(self, label: str, start: int, end: int):
        """Build an ``on_progress(done, total)`` callback for one phase.

        Maps the phase's record progress onto the bar band ``[start, end)`` and emits
        a throttled update carrying a live "x of N" count. Whole side-channel: wrapped
        so a progress hiccup can never interrupt the import, and monotonic so the bar
        only ever moves forward."""
        state = {"pct": start, "done": 0}

        def cb(done: int, total: int) -> None:
            try:
                if not total:
                    return
                # Clamp into the band; the end is reserved for the next phase's start.
                pct = start + (end - start) * done // total
                if pct >= end:
                    pct = end - 1
                advanced = pct > state["pct"]
                stepped = done - state["done"] >= self._PROGRESS_EVERY
                if not advanced and not stepped:
                    return
                state["pct"] = max(pct, state["pct"])
                state["done"] = done
                self._progress(
                    state["pct"],
                    f"Importing {label.lower()} {done:,} of {total:,}...")
            except Exception:
                pass

        return cb

    def _progress(self, pct: int, description: str = "") -> None:
        # A custom realtime event (not frappe.publish_progress) so only the wizard's
        # own step-5 bar updates - publish_progress also triggers Frappe's native
        # progress dialog, which double-rendered on top of our bar.
        desc = description or self.STEPS.get(pct, "")
        frappe.publish_realtime(
            "tally_migration_progress",
            {
                "title": "Tally Masters Migration",
                "percent": pct,
                "description": desc,
            },
            user=frappe.session.user,
        )
        # Persist the latest progress so a reloaded/reconnected page can recover it by
        # polling (see api.run_progress). Realtime is best-effort - a page that reloads
        # mid-run misses past events - so the poll is the reliable source of truth and
        # this is what stops the reconnected bar from sitting at 0%.
        # Best-effort: progress reporting is cosmetic and runs inside the migration's
        # critical path, so a cache/Redis hiccup must never abort an otherwise-good run.
        if self.log:
            try:
                frappe.cache().set_value(
                    f"tally_migration_progress:{self.log.name}",
                    {"percent": pct, "description": desc},
                    expires_in_sec=6 * 60 * 60,
                )
            except Exception:
                pass

    def _record_uom_edits(self) -> None:
        """Fold the pre-flight UOM resolutions into the applied-edits audit trail.

        Resolving a unit on the Check screen is a real pre-flight decision, like a
        per-record field fix, but UOM choices travel on their own channel
        (``uom_overrides`` / ``created_uoms``) and so were absent from the audit
        table. We append them here (after the field edits) so the log shows every
        change the user made. On a re-run ``created_uoms`` isn't replayed, but by then
        those units exist, so they read as "Mapped to existing unit" - accurate for
        that run. See :func:`uom_edits` for the row shape and exclusions.
        """
        self.applied_edits.extend(uom_edits(self.uom_overrides, self.created_uoms))

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
            self.log.extracted_counts = frappe.as_json(
                {**masters.summary, **coa.summary, "_phase_seconds": self._timings})
            self.log.import_summary = frappe.as_json(summary.as_dict())
            self.log.applied_edits = frappe.as_json(self.applied_edits)
            manifest = summary.created_records()
            self._track_wizard_uoms(manifest)
            self.log.created_records = frappe.as_json(manifest)
            self.log.reconciliation_report = frappe.as_json(self._reconciliation(masters, coa))
            self.log.set("errors", [])
            if summary.has_errors or summary.has_warnings:
                self.log.error_log = summary.error_lines()
                for row in summary.error_records():
                    self.log.append("errors", row)
            self.log.save(ignore_permissions=True)
            frappe.db.commit()
        except Exception as exc:
            frappe.log_error(f"Migration log finalize failed: {exc}", "Tally Migrator")

    def _track_wizard_uoms(self, manifest: dict) -> None:
        """Fold the pre-flight "create as new" UOMs into the manifest's Units entry so
        revert undoes them.

        These are inserted by the create_uoms endpoint BEFORE the run, so the unit
        importer skips them as already-existing and never records them - leaving them
        orphaned by revert. Only names the wizard reported creating are added, never
        a pre-existing UOM (create_uoms returns those under "existing", not here), and
        only if the UOM still exists. Revert deletes them unforced, so one still
        referenced by a non-migration item is safely kept rather than force-removed."""
        if not self.created_uoms:
            return
        units = manifest.setdefault("Units", [])
        already = {(d.get("doctype"), d.get("name")) for d in units}
        for name in self.created_uoms:
            if ("UOM", name) not in already and frappe.db.exists("UOM", name):
                units.append({"doctype": "UOM", "name": name})

    def _reconciliation(self, masters: ExtractedMasters, coa) -> dict:
        """Read-only post-import reconciliation summary (Tally figures vs ERPNext).

        Best-effort: a failure here returns an empty dict so the rest of the log
        still finalizes - the summary is informational, never a gate."""
        try:
            from tally_migrator.migration.reconciliation import build_reconciliation
            rec = build_reconciliation(
                self.config.erpnext_company, self.importer.abbr, coa, masters)
            self._flag_cumulative_openings(rec)
            return rec
        except Exception as exc:
            frappe.log_error(f"Reconciliation summary failed: {exc}", "Tally Migrator")
            return {}

    def _flag_cumulative_openings(self, rec: dict) -> None:
        """Distinguish 'cumulative across exports' from a genuine per-file mismatch.

        The reconciliation compares THIS file's openings (Tally column) against the
        whole company's openings read back from the GL (ERPNext column). When more
        than one *different* Tally export has been imported into the same company,
        the ERPNext column is the running total of all of them, so Receivables /
        Payables can never line up with a single file - and that is expected, not a
        data error. We only want the heads-up in exactly that case, never when a real
        figure diverges within a single-export company. So gate it on BOTH signals:
        (a) a Receivables/Payables row actually diverges, AND (b) a prior Completed
        log for this company imported a different source file. Either alone is not
        enough - (a) without (b) is a true mismatch (keep the red alert); (b) without
        (a) reconciled fine, so there is nothing to explain.
        """
        if not rec or not rec.get("available") or not self.log:
            return
        diverged = any(
            row.get("has_erpnext") and not row.get("match")
            and row.get("key") in ("receivables", "payables")
            for row in rec.get("rows", []))
        if not diverged:
            return
        current_file = self.config.source_file or ""
        others = frappe.get_all(
            "Tally Migration Log",
            filters={
                "company": self.config.erpnext_company,
                "status": ["in", ["Completed", "Completed with Errors"]],
                "name": ["!=", self.log.name],
                "source_file": ["not in", ["", current_file]],
            },
            pluck="source_file")
        files = sorted({f for f in others if f})
        if files:
            rec["cumulative_openings"] = True
            rec["other_exports"] = files

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
