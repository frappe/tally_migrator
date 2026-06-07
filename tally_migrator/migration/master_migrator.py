from dataclasses import dataclass
from typing import Callable

import frappe

from tally_migrator.tally.config import TallyConfig
from tally_migrator.tally.extractors import TallyExtractor, ExtractedMasters
from tally_migrator.erpnext.importers import ERPNextImporter, ImportResult
from tally_migrator.migration.overrides import apply_record_overrides


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
    entity) means adding a new entity to the pipeline needs no change here — the
    summary, the log, and the error reporting all iterate generically.
    """

    def __init__(self, results: dict[str, ImportResult]):
        self.results = results

    def as_dict(self) -> dict:
        return {label: result.as_dict() for label, result in self.results.items()}

    @property
    def has_errors(self) -> bool:
        return any(result.failed > 0 for result in self.results.values())

    def error_lines(self) -> str:
        """Flat, human-readable list of per-record failures for the log."""
        return "\n".join(
            f"[{label}] {e['name']}: {e['reason']}"
            for label, result in self.results.items()
            for e in result.errors
        )

    def error_records(self) -> list[dict]:
        """Structured per-record failures for the log's child table."""
        return [
            {"record_type": label, "record_name": e["name"], "reason": e["reason"]}
            for label, result in self.results.items()
            for e in result.errors
        ]


class MasterMigrator:
    """
    Phase 1 orchestrator: Tally Masters → ERPNext.

    Order of operations
    -------------------
    Warehouses first  — Items reference warehouses.
    Customers / Suppliers next — independent of each other.
    Items last — depend on Item Groups and Warehouses.

    A ``Tally Migration Log`` is created with status ``Running`` *before* any
    work starts and finalized at the end, so an interrupted run still leaves an
    auditable record. Progress is published to the Frappe realtime bus for an
    optional live progress bar (best-effort; the call also returns the summary).

    Scaling note: for very large datasets, ``run`` is a natural unit to move into
    a background job (``frappe.enqueue``) keyed off the log document. It is kept
    synchronous here for reliability — the summary is returned directly rather
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
                 record_overrides: dict | None = None):
        """``source`` is any object exposing ``ping()`` + ``get_collection``.

        In practice this is a :class:`FileTallySource` wrapping an uploaded
        Tally masters XML export.

        ``record_overrides`` are per-record field fixes from the pre-flight screen,
        applied to the extracted records in memory before import.
        """
        self.config = config
        self.client = source
        self.extractor = TallyExtractor(self.client)
        self.importer = ERPNextImporter(config.erpnext_company, uom_overrides=uom_overrides or {})
        self.record_overrides = record_overrides or {}
        self.log = None

    # ── Public ────────────────────────────────────────────────────────────────

    def run(self) -> MigrationSummary:
        self.log = self._create_log()
        try:
            self._progress(0)
            if not self.client.ping():
                frappe.throw("Could not read the uploaded file. Please re-upload a valid Tally Masters XML export.")

            self._progress(10)
            masters = self.extractor.extract_all()
            apply_record_overrides(masters, self.record_overrides)
            frappe.logger().info(f"[Tally Migrator] Extracted: {masters.summary}")

            results: dict[str, ImportResult] = {}
            for step in self._pipeline(masters):
                self._progress(step.percent, f"Importing {len(step.records)} {step.label.lower()}...")
                results[step.label] = step.importer(step.records)
            summary = MigrationSummary(results)

            self._progress(95)
            self._finalize_log(masters, summary)

            self._progress(100)
            return summary
        except Exception as exc:
            self._fail_log(exc)
            raise

    def _pipeline(self, masters: ExtractedMasters) -> list[PipelineStep]:
        """Entity import order. Adding an entity = add one step here.

        Warehouses first (Items reference them); Customers/Suppliers next
        (independent); Items last (depend on Item Groups + Warehouses).
        """
        return [
            PipelineStep("Warehouses", 25, self.importer.import_warehouses, masters.warehouses),
            PipelineStep("Customers", 45, self.importer.import_customers, masters.customers),
            PipelineStep("Suppliers", 60, self.importer.import_suppliers, masters.suppliers),
            PipelineStep("Items", 75, self.importer.import_items, masters.items),
        ]

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
        log.insert(ignore_permissions=True)
        frappe.db.commit()
        return log

    def _finalize_log(self, masters: ExtractedMasters, summary: MigrationSummary) -> None:
        """Record extraction/import results. Must never abort the migration."""
        try:
            self.log.reload()
            self.log.status = "Completed with Errors" if summary.has_errors else "Completed"
            self.log.extracted_counts = frappe.as_json(masters.summary)
            self.log.import_summary = frappe.as_json(summary.as_dict())
            self.log.set("errors", [])
            if summary.has_errors:
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
