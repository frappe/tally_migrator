from dataclasses import dataclass

import frappe

from tally_migrator.tally.client import TallyClient, TallyConfig
from tally_migrator.tally.extractors import TallyExtractor, ExtractedMasters
from tally_migrator.erpnext.importers import ERPNextImporter, ImportResult


@dataclass
class MigrationSummary:
    warehouses: ImportResult
    customers: ImportResult
    suppliers: ImportResult
    items: ImportResult

    def _pairs(self):
        return [
            ("Warehouses", self.warehouses),
            ("Customers", self.customers),
            ("Suppliers", self.suppliers),
            ("Items", self.items),
        ]

    def as_dict(self) -> dict:
        return {label: result.as_dict() for label, result in self._pairs()}

    @property
    def has_errors(self) -> bool:
        return any(result.failed > 0 for _, result in self._pairs())

    def error_lines(self) -> str:
        """Flat, human-readable list of per-record failures for the log."""
        lines = [
            f"[{label}] {e['name']}: {e['reason']}"
            for label, result in self._pairs()
            for e in result.errors
        ]
        return "\n".join(lines)


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
        0: "Connecting to Tally...",
        10: "Extracting data from Tally...",
        25: "Importing Warehouses...",
        45: "Importing Customers...",
        60: "Importing Suppliers...",
        75: "Importing Items...",
        95: "Saving migration log...",
        100: "Migration complete.",
    }

    def __init__(self, config: TallyConfig, source=None):
        """``source`` is any object exposing ``ping()`` + ``get_collection``.

        Defaults to a live :class:`TallyClient`. Pass a
        :class:`FileTallySource` to run the same pipeline against an uploaded
        Tally masters XML export instead of a live connection.
        """
        self.config = config
        self.client = source or TallyClient(config)
        self.extractor = TallyExtractor(self.client)
        self.importer = ERPNextImporter(config.erpnext_company)
        self.log = None

    # ── Public ────────────────────────────────────────────────────────────────

    def run(self) -> MigrationSummary:
        self.log = self._create_log()
        try:
            self._progress(0)
            if not self.client.ping():
                frappe.throw(
                    f"Cannot connect to Tally at {self.config.url}. "
                    "Ensure Tally is open and the HTTP server is enabled on port 9000."
                )

            self._progress(10)
            masters = self.extractor.extract_all()
            frappe.logger().info(f"[Tally Migrator] Extracted: {masters.summary}")

            self._progress(25, f"Importing {len(masters.warehouses)} warehouses...")
            warehouses = self.importer.import_warehouses(masters.warehouses)

            self._progress(45, f"Importing {len(masters.customers)} customers...")
            customers = self.importer.import_customers(masters.customers)

            self._progress(60, f"Importing {len(masters.suppliers)} suppliers...")
            suppliers = self.importer.import_suppliers(masters.suppliers)

            self._progress(75, f"Importing {len(masters.items)} items...")
            items = self.importer.import_items(masters.items)

            summary = MigrationSummary(
                warehouses=warehouses, customers=customers, suppliers=suppliers, items=items
            )

            self._progress(95)
            self._finalize_log(masters, summary)

            self._progress(100)
            return summary
        except Exception as exc:
            self._fail_log(exc)
            raise

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
            if summary.has_errors:
                self.log.error_log = summary.error_lines()
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
