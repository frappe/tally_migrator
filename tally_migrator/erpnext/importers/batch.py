"""Batch importer: Tally batch-wise opening detail -> ERPNext Batch masters."""

from datetime import datetime

import frappe

from tally_migrator.naming import safe_item_code
from tally_migrator.tally.extractors import TallyExtractor
from .base import ImportResult


class BatchImporter:
    """Create ERPNext Batch masters from Tally's batch-wise opening detail.

    A batch-tracked Tally item (``ISBATCHWISEON=Yes``) carries its opening stock
    per batch inside ``BATCHALLOCATIONS.LIST`` (extracted onto each item as
    ``GodownOpenings`` rows with a ``batch`` / ``mfg_date`` / ``expiry``). Each
    distinct batch becomes an ERPNext Batch (``batch_id`` = the Tally batch name,
    linked to the item, with manufacturing/expiry dates), so the batch-wise opening
    Stock Reconciliation can post against a real Batch.

    Runs after Items (the item must exist and carry ``has_batch_no``) and before
    Opening Stock (the reconciliation references the batch). Idempotent: a Batch
    whose id already exists is skipped. ``batch_id`` is global in ERPNext, so a name
    already used by a *different* item is skipped with a warning rather than
    hijacked.
    """

    doctype = "Batch"

    def __init__(self, company: str, abbr: str):
        self.company = company
        self.abbr = abbr

    def run(self, items: list[dict]) -> ImportResult:
        result = ImportResult(self.doctype)
        for it in items:
            # Tally stamps every item's opening with an implicit "Primary Batch", so
            # only genuinely batch-tracked items (ISBATCHWISEON=Yes) get Batch masters -
            # otherwise we'd create a batch for every non-batch item too.
            if (it.get("IsBatchWiseOn") or "").strip().lower() != "yes":
                continue
            code = safe_item_code(it.get("_name", ""))
            seen: set[str] = set()
            for row in (it.get("GodownOpenings") or []):
                batch = (row.get("batch") or "").strip()
                if not batch or batch in seen:
                    continue
                seen.add(batch)
                self._upsert(result, code, it.get("_name", ""), batch, row)
        return result

    def _upsert(self, result: ImportResult, item_code: str, item_name: str,
                batch_id: str, row: dict) -> None:
        if not frappe.db.exists("Item", item_code):
            return                       # item didn't import (warned by ItemImporter)
        existing_item = frappe.db.get_value("Batch", batch_id, "item")
        if existing_item is not None:
            # Batch ids are global in ERPNext. Same item → already created (idempotent);
            # different item → a genuine cross-item id clash we must not hijack.
            if existing_item != item_code:
                result.add_warning(
                    item_name,
                    f"batch '{batch_id}' already exists for a different item "
                    f"('{existing_item}'); skipped. Tally batch names are per-item but "
                    "ERPNext batch ids are global - rename one to migrate both.")
            else:
                result.skipped += 1
            return
        try:
            doc = frappe.get_doc({
                "doctype": "Batch",
                "batch_id": batch_id,
                "item": item_code,
                "manufacturing_date": TallyExtractor._parse_tally_date(row.get("mfg_date", "")) or None,
                "expiry_date": self._parse_expiry(row.get("expiry", "")) or None,
            })
            doc.flags.ignore_mandatory = True
            doc.insert(ignore_permissions=True)
            frappe.db.commit()
            result.add_created(doc.name)
        except Exception as exc:
            frappe.db.rollback()
            result.add_error(f"{item_name} (batch {batch_id})", exc)

    @staticmethod
    def _parse_expiry(raw: str) -> str:
        """Tally batch ``EXPIRYPERIOD`` text ("31-Jul-26") → ISO "2026-07-31".

        Returns "" for a blank or unparseable value so the batch still posts with no
        expiry rather than failing."""
        s = (raw or "").strip()
        if not s:
            return ""
        for fmt in ("%d-%b-%y", "%d-%b-%Y", "%d-%m-%Y", "%Y%m%d"):
            try:
                return datetime.strptime(s, fmt).date().isoformat()
            except ValueError:
                continue
        return ""
