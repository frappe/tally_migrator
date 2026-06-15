"""Unit importer: Tally Units -> ERPNext UOM and conversion factors."""

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
)
from tally_migrator.naming import safe_item_code, company_scoped
from tally_migrator.tally.extractors import TallyExtractor
from tally_migrator.validation.engine import (
    infer_gst_category, validate_gstin, GSTIN_STATE_CODES,
)
from .base import BaseImporter, ImportResult

# ── Unit importer (Tally Units → ERPNext UOM + conversion factors) ────────────

class UnitImporter:
    """Create ERPNext UOMs (and compound conversions) from Tally Unit masters.

    Today UOMs are only resolved by name when an item is imported; the Unit
    masters themselves (formal name, decimal places, compound relations) are never
    read. This imports them so a UOM carries Tally's formal name + whole-number
    flag, and compound units (e.g. 1 Doz = 12 Nos) become a UOM Conversion Factor.

    Conversion-factor creation is best-effort (it depends on the constituent UOMs
    and a UOM Category): any failure is a non-fatal warning, never a hard error.
    """

    doctype = "UOM"

    def __init__(self, company: str, abbr: str):
        self.company = company
        self.abbr = abbr

    def run(self, units: list[dict]) -> ImportResult:
        result = ImportResult(self.doctype)
        for u in units:
            self._ensure_uom(result, u)
        # Compound conversions need both constituent UOMs to exist first, so do
        # them in a second pass after every simple UOM is created.
        for u in units:
            if not self._is_simple(u):
                self._ensure_conversion(result, u)
        return result

    @staticmethod
    def _is_simple(u: dict) -> bool:
        return (u.get("IsSimpleUnit") or "").strip().lower() not in ("no", "false", "0")

    def _ensure_uom(self, result: ImportResult, u: dict) -> None:
        name = u["_name"]
        try:
            if frappe.db.exists("UOM", name):
                result.skipped += 1
                return
            decimals = (u.get("DecimalPlaces") or "").strip()
            doc = frappe.get_doc({
                "doctype": "UOM",
                "uom_name": name,
                # Tally decimalplaces=0 → quantity must be whole.
                "must_be_whole_number": 1 if decimals in ("", "0") else 0,
            })
            doc.insert(ignore_permissions=True)
            frappe.db.commit()
            result.add_created(doc.name)
        except Exception as exc:
            result.add_error(name, exc)
            frappe.db.rollback()

    def _ensure_conversion(self, result: ImportResult, u: dict) -> None:
        """Compound unit '1 BaseUnits = Conversion AdditionalUnits' → UOM
        Conversion Factor. Non-fatal: warn if it can't be created."""
        base = (u.get("BaseUnits") or "").strip()
        additional = (u.get("AdditionalUnits") or "").strip()
        factor = BaseImporter._to_float(u.get("Conversion"))
        if not (base and additional and factor > 0):
            return
        try:
            exists = frappe.db.exists(
                "UOM Conversion Factor", {"from_uom": base, "to_uom": additional})
            if exists:
                return
            doc = {
                "doctype": "UOM Conversion Factor",
                "from_uom": base,
                "to_uom": additional,
                "value": factor,
            }
            category = self._uom_category(result)
            if category:
                doc["category"] = category
            frappe.get_doc(doc).insert(ignore_permissions=True)
            frappe.db.commit()
        except Exception as exc:
            frappe.log_error("Tally Migrator", f"UOM conversion failed for {u['_name']}: {exc}")
            result.add_warning(
                u["_name"],
                f"compound unit conversion (1 {base} = {factor:g} {additional}) "
                f"not created: {exc}")
            frappe.db.rollback()

    @staticmethod
    def _uom_category(result: "ImportResult | None" = None) -> str:
        """A UOM Category to attach conversions to (required in recent ERPNext).
        Reuse one if present, else create a 'Tally Imported' category.

        Creating the category is a side effect the user didn't explicitly ask for,
        so when we do create one we record a one-off warning (the category is reused
        thereafter, so this fires at most once per run) - the auto-created master is
        then visible/auditable in the log rather than appearing silently."""
        if not frappe.db.has_column("UOM Conversion Factor", "category"):
            return ""
        existing = frappe.get_all("UOM Category", pluck="name", limit=1)
        if existing:
            return existing[0]
        try:
            cat = frappe.get_doc({"doctype": "UOM Category", "category_name": "Tally Imported"})
            cat.insert(ignore_permissions=True)
            frappe.db.commit()
            if result is not None:
                result.add_warning(
                    "UOM Category",
                    "auto-created a 'Tally Imported' UOM Category to hold compound-unit "
                    "conversions - ERPNext requires every conversion to belong to a "
                    "category and none existed.")
            return cat.name
        except Exception:
            frappe.db.rollback()
            return ""
