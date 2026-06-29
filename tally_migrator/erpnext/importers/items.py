"""Item importer: ensures Item Groups and maps UOM."""

import contextlib
from collections import Counter
from dataclasses import dataclass, field

import frappe

from tally_migrator.tally.mappings import (
    UOM_MAP,
    TALLY_STATE_MAP,
    DEFAULT_CUSTOMER_GROUP,
    DEFAULT_SUPPLIER_GROUP,
    DEFAULT_ITEM_GROUP,
    DEFAULT_TERRITORY,
    DEFAULT_WAREHOUSE,
    DEFAULT_UOM,
    ERPNEXT_ROOT_GROUPS,
    classify_group,
    gst_category_from_type,
    is_stock_item,
)
from tally_migrator.naming import safe_item_code, company_scoped
from tally_migrator.tally.extractors import TallyExtractor
from tally_migrator.validation.engine import (
    infer_gst_category, validate_gstin, GSTIN_STATE_CODES,
)
from .base import BaseImporter, ImportResult

# ── Item importer ───────────────────────────────────────────────────────────────

class ItemImporter(BaseImporter):
    doctype = "Item"
    key_field = "item_code"

    def __init__(self, company: str, abbr: str, uom_overrides: dict | None = None):
        super().__init__(company, abbr)
        self._uom_overrides = uom_overrides or {}

    def before_run(self, records: list[dict], result: ImportResult) -> None:
        self._ensure_item_groups({r.get("Parent") for r in records if r.get("Parent")}, result)
        # Two distinct Tally items can collapse to the same ERPNext item_code once
        # it is truncated to 140 chars and '/' is replaced. The generic upsert keys
        # on item_code, so the later one is skipped as "already there" - which looks
        # like a harmless duplicate. Flag the collision here so the dropped item is
        # visible in the log, matching the pre-flight ITEM_CODE_COLLISION check.
        for code, names in self._code_collisions(records).items():
            kept, dropped = names[0], names[1:]
            for name in dropped:
                result.add_warning(
                    name,
                    f"item not imported - its code '{code}' collides with item "
                    f"'{kept}' (both reduce to the same ERPNext item_code after "
                    "truncation/'/' replacement). Rename one in Tally and re-run.")
        # Surface any GST treatment we couldn't map so the loss is auditable rather
        # than silently defaulting the item to taxable.
        for r in records:
            raw = (r.get("GSTTaxability") or "").strip()
            if raw and self._gst_treatment(raw) is None:
                result.add_warning(
                    r["_name"],
                    f"GST type '{raw}' not recognised - item imported as taxable; "
                    "set its GST treatment manually if needed.")
        # India Compliance check: the item-level GST fields (HSN code, taxability,
        # supply type, nil/exempt/non-GST flags) only exist when the India Compliance
        # app is installed. Without it ERPNext core silently drops those keys, so we
        # say so once instead of pretending the GST data landed.
        self._india_compliance = "india_compliance" in frappe.get_installed_apps()
        # Resolve each item's GST Item Tax Template once (links the rate to the
        # India-Compliance-seeded "GST 18%" / "Nil-Rated" templates for this
        # company). Done here, with access to ``result``, so a missing template
        # warns exactly once; build_doc just reads the resolved map.
        self._resolve_tax_templates(records, result)
        if not self._india_compliance and any(
            (r.get("GSTTaxability") or r.get("HSNCode")
             or r.get("GstApplicable") or r.get("TypeOfSupply"))
            for r in records
        ):
            result.add_warning(
                "GST details",
                "Your items carry GST data (HSN code, taxability, supply type), but the "
                "India Compliance app is not installed on this site - ERPNext core has no "
                "fields to store it, so these attributes were skipped. To import them, "
                "install 'India Compliance' from the Frappe Cloud marketplace and re-run "
                "this migration; everything else imported normally.")

    @staticmethod
    def _code_collisions(records: list[dict]) -> dict:
        """``{item_code: [name, ...]}`` for codes produced by >1 distinct Tally
        name. Order preserved so the first occurrence is treated as the one kept."""
        by_code: dict = {}
        for r in records:
            name = r.get("_name")
            if not name:
                continue
            by_code.setdefault(safe_item_code(name), [])
            if name not in by_code[safe_item_code(name)]:
                by_code[safe_item_code(name)].append(name)
        return {code: names for code, names in by_code.items() if len(names) > 1}

    def _resolve_uom(self, tally_uom: str) -> str:
        """Resolve a Tally base unit to an ERPNext UOM, matching what the pre-flight
        Check step tells the user: a user override, then the built-in UOM_MAP, then
        the Tally unit's OWN name when a UOM by that name exists (the Unit importer
        creates it earlier in the same run), and only DEFAULT_UOM as a last resort.

        Previously this skipped straight to DEFAULT_UOM for any unit not in UOM_MAP,
        which contradicted the resolver (uom_resolver: ``UOM_MAP.get(u, u)``) - so an
        item whose unit (e.g. 'Ream') existed but wasn't in UOM_MAP silently became
        'Nos', and its price levels / BOM components then mismatched the item."""
        if not tally_uom:
            return DEFAULT_UOM
        override = self._uom_overrides.get(tally_uom)
        if override:
            return override
        if tally_uom in UOM_MAP:
            return UOM_MAP[tally_uom]
        if frappe.db.exists("UOM", tally_uom):
            return tally_uom
        return DEFAULT_UOM

    def build_doc(self, record: dict) -> dict:
        uom = self._resolve_uom((record.get("BaseUnits") or "").strip())
        doc = {
            "doctype": "Item",
            "item_code": safe_item_code(record["_name"]),
            "item_name": record["_name"],
            "item_group": record.get("Parent") or DEFAULT_ITEM_GROUP,
            "stock_uom": uom,
            "description": record.get("Description") or record["_name"],
            "is_stock_item": self._is_stock_item(record),
            "standard_rate": self._to_float(record.get("StandardPrice")),
            "valuation_rate": self._to_float(record.get("StandardCost")),
            "gst_hsn_code": record.get("HSNCode") or "",
        }
        vm = self._valuation_method(record)
        if vm:
            doc["valuation_method"] = vm
        # Batch / expiry tracking. ERPNext models expiry per batch, so has_expiry_date
        # is only valid alongside has_batch_no - gate it so a perishable-but-not-batch
        # Tally item never produces an invalid Item. Tally batches are pre-existing, so
        # we do NOT auto-create new ones; the Batch importer recreates the Tally batches.
        if self._yes(record.get("IsBatchWiseOn")):
            doc["has_batch_no"] = 1
            if self._yes(record.get("IsPerishableOn")):
                doc["has_expiry_date"] = 1
        doc.update(self._gst_fields(record))
        tpl = getattr(self, "_tax_templates", {}).get(record["_name"])
        if tpl:
            doc["taxes"] = [{"item_tax_template": tpl}]
        return doc

    # ── GST rate -> Item Tax Template linking (India Compliance) ───────────────
    @staticmethod
    def _gst_treatment_label(record: dict) -> "str | None":
        """ERPNext/India-Compliance gst_treatment string for an item, or None when
        the Tally GST type is unrecognised (already warned elsewhere)."""
        if (record.get("GstApplicable") or "").strip().lower() in ("not applicable", "no"):
            return "Non-GST"
        key = (record.get("GSTTaxability") or "").strip().lower().replace("-", " ").replace("_", " ")
        key = " ".join(key.split())
        return {
            "": "Taxable", "taxable": "Taxable", "applicable": "Taxable",
            "nil rated": "Nil-Rated", "nil": "Nil-Rated",
            "exempt": "Exempted", "exempted": "Exempted",
            "non gst": "Non-GST", "not applicable": "Non-GST",
        }.get(key)

    def _resolve_tax_template(self, treatment: str, rate: float) -> "str | None":
        """The company's GST Item Tax Template for this treatment/rate, matched by
        the India-Compliance gst_treatment + gst_rate fields (never by the
        company-suffixed name), or None when none is seeded."""
        filters = {"company": self.company, "gst_treatment": treatment}
        if treatment == "Taxable":
            if not rate:
                return None
            filters["gst_rate"] = rate
        rows = frappe.get_all(
            "Item Tax Template", filters=filters, pluck="name", limit=1,
            ignore_permissions=True)
        return rows[0] if rows else None

    def _resolve_tax_templates(self, records: list[dict], result: ImportResult) -> None:
        """Build ``{item name: template}`` for build_doc, warning once per item when
        a taxable rate has no matching template. No-op without India Compliance
        (its gst_rate/gst_treatment fields, and the seeded templates, don't exist)."""
        self._tax_templates: dict = {}
        if not getattr(self, "_india_compliance", False):
            return
        for r in records:
            treatment = self._gst_treatment_label(r)
            if not treatment:
                continue
            rate = self._to_float(r.get("GstRate"))
            tpl = self._resolve_tax_template(treatment, rate)
            if tpl:
                self._tax_templates[r["_name"]] = tpl
            elif treatment == "Taxable" and rate:
                result.add_warning(
                    r["_name"],
                    f"no GST Item Tax Template for {rate:g}% on company "
                    f"'{self.company}' - item imported without a tax rate; create a "
                    f"'GST {rate:g}%' template or set it on the item manually.")

    def recover_insert(self, data: dict, exc: Exception):
        """India Compliance makes ``gst_hsn_code`` a Link to "GST HSN Code" and
        rejects an Item whose code is not a known HSN - a check that fires even with
        the validate-HSN setting off. Retry once with the HSN cleared so the item
        still lands; the user adds a valid HSN later. Matched on the error text
        because the failure surfaces as a LinkValidationError / India-Compliance
        message, both of which name HSN. A blank HSN can't be salvaged this way
        (nothing to clear), so we only retry when one was actually set."""
        hsn = (data.get("gst_hsn_code") or "").strip()
        if not hsn or "hsn" not in str(exc).lower():
            return None
        retry = dict(data)
        retry["gst_hsn_code"] = ""
        return retry, (
            f"imported without HSN code - ERPNext rejected '{hsn}' (not a valid GST "
            "HSN Code). Add a correct HSN before raising GST invoices.")

    @staticmethod
    def _valuation_method(record: dict):
        """Map Tally's costing/valuation method to ERPNext's Item.valuation_method.

        Tally "Avg. Cost"/"Avg. Price" → "Moving Average"; FIFO/LIFO map straight
        across. Returns None for anything else, so the item keeps the ERPNext
        default (inherited from Stock Settings) rather than getting an invalid value.
        """
        raw = (record.get("ValuationMethod") or "").strip().lower()
        if not raw:
            return None
        if raw.startswith("fifo") or "first in" in raw:
            return "FIFO"
        if raw.startswith("lifo") or "last in" in raw:
            return "LIFO"
        if "avg" in raw or "average" in raw or "moving" in raw:
            return "Moving Average"
        return None

    @staticmethod
    def _yes(val) -> bool:
        """Tally boolean flag → bool. Tally writes 'Yes'/'No'; anything else is False."""
        return (val or "").strip().lower() == "yes"

    @staticmethod
    def _is_stock_item(record: dict) -> int:
        """A Tally Stock Item whose GST supply type is 'Services' maps to a
        non-stock Item in ERPNext (read from GSTDETAILS.LIST/SUPPLYTYPE); every
        other supply type stays a stock item. Delegates to the shared, pure rule
        (mappings.is_stock_item) so the reconciliation counts stock for exactly
        the items posted here."""
        return is_stock_item(record)

    @staticmethod
    def _gst_treatment(gst_type: str):
        """Map a Tally GST type to ERPNext flags, or None when unrecognised.

        Returns a dict of India-Compliance Item flags to set. Empty dict = taxable
        (the default); None = we don't know this value (caller warns)."""
        key = (gst_type or "").strip().lower().replace("-", " ").replace("_", " ")
        key = " ".join(key.split())
        table = {
            "": {},
            "taxable": {},
            "applicable": {},
            "nil rated": {"is_nil_exempt": 1},
            "nil": {"is_nil_exempt": 1},
            "exempt": {"is_nil_exempt": 1},
            "exempted": {"is_nil_exempt": 1},
            "non gst": {"is_non_gst": 1},
            "not applicable": {"is_non_gst": 1},
        }
        return table.get(key)

    def _gst_fields(self, record: dict) -> dict:
        """Item-level GST attributes derived from Tally's GST taxability
        (GSTDETAILS.LIST/TAXABILITY: Taxable / Nil Rated / Exempt / Non-GST).

        ``is_nil_exempt`` / ``is_non_gst`` are India-Compliance fields; setting them
        on the doc is harmless when that app isn't installed (Frappe ignores keys
        that aren't real docfields). Unrecognised values fall back to taxable and
        are flagged as a warning in ``before_run``."""
        # A flat "GST Applicable = Not Applicable" overrides taxability → non-GST.
        if (record.get("GstApplicable") or "").strip().lower() in ("not applicable", "no"):
            return {"is_non_gst": 1}
        return self._gst_treatment(record.get("GSTTaxability") or "") or {}

    def _ensure_item_groups(self, groups: set[str], result: ImportResult) -> None:
        """Create any missing Item Groups under the default parent group.

        A failure here means items in that group will fall back to the default
        group, so it's recorded as a warning (visible loss of grouping) rather
        than failing silently. A group we create is recorded on the manifest so
        revert removes it too - the items that reference it are deleted first (same
        bucket, reversed order) and the delete is unforced, so a group still used by
        an item outside this run is kept."""
        for group in groups:
            if group and not frappe.db.exists("Item Group", group):
                try:
                    ig = frappe.new_doc("Item Group")
                    ig.item_group_name = group
                    ig.parent_item_group = DEFAULT_ITEM_GROUP
                    ig.insert(ignore_permissions=True)
                    frappe.db.commit()
                    result.add_created(ig.name, "Item Group")
                except Exception as exc:
                    frappe.log_error("Tally Migrator", f"Item Group creation failed: {group}: {exc}")
                    result.add_warning(group, f"item group not created: {exc}")
