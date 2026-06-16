"""BOM importer: Tally multi-component lists -> ERPNext BOM (submitted, active)."""

import frappe

from tally_migrator.naming import safe_item_code
from .base import BaseImporter, ImportResult


class BomImporter:
    """Create ERPNext BOMs from Tally bills of materials.

    Each Tally BOM becomes a submitted, active BOM; the first BOM per item is the
    default. Runs after Items so the finished item and components exist. Only
    NATUREOFITEM=Component rows are migrated; by-products/co-products/scrap are
    skipped with a warning (no fixture coverage to build secondary_items against).

    Idempotent by skip-if-exists: BOM names auto-increment, so a re-run would
    create duplicates; if any BOM already exists for an item we skip it entirely.
    """

    doctype = "BOM"

    def __init__(self, company: str, abbr: str):
        self.company = company
        self.abbr = abbr
        self.currency = frappe.get_cached_value(
            "Company", company, "default_currency") or "INR"

    def run(self, items: list[dict]) -> ImportResult:
        result = ImportResult(self.doctype)
        for it in items:
            boms = it.get("Boms") or []
            if not boms:
                continue
            code = safe_item_code(it.get("_name", ""))
            if not frappe.db.exists("Item", code):
                continue                         # finished item didn't import
            if frappe.db.exists("BOM", {"item": code}):
                result.skipped += 1              # idempotent: never duplicate
                continue
            for i, bom in enumerate(boms):
                self._import_bom(result, code, it.get("_name", ""), bom, is_default=(i == 0))
        return result

    def _import_bom(self, result, code, item_name, bom, is_default):
        rows, warns = self._component_rows(code, bom.get("components") or [])
        for w in warns:
            result.add_warning(item_name, w)
        if not rows:
            result.add_warning(item_name, "BOM skipped - no usable Component rows.")
            return
        qty, uom = self._parse_qty_uom(bom.get("basic_qty"))
        doc = {
            "doctype": "BOM", "item": code, "company": self.company,
            "quantity": qty or 1, "currency": self.currency, "conversion_rate": 1,
            "is_active": 1, "is_default": 1 if is_default else 0, "items": rows,
        }
        if uom and frappe.db.exists("UOM", uom):
            doc["uom"] = uom
        try:
            d = frappe.get_doc(doc)
            d.insert(ignore_permissions=True)
            d.submit()
            frappe.db.commit()
            result.add_created(d.name, "BOM")
        except Exception as exc:
            frappe.db.rollback()
            result.add_error(f"{item_name} (BOM {bom.get('name')})", exc)

    def _component_rows(self, finished_code, components):
        rows, warns = [], []
        for c in components:
            nature = (c.get("natureofitem") or "Component").strip()
            cname = (c.get("stockitemname") or "").strip()
            if nature.lower() != "component":
                warns.append(f"component '{cname}' is a {nature}, not migrated "
                             "(only Components are imported into the BOM).")
                continue
            ccode = safe_item_code(cname)
            if not frappe.db.exists("Item", ccode):
                warns.append(f"BOM component '{cname}' not found as an item - skipped.")
                continue
            if ccode == finished_code:
                warns.append(f"BOM component '{cname}' is the finished item itself "
                             "(self-reference) - skipped.")
                continue
            qty, uom = self._parse_qty_uom(c.get("actualqty"))
            if not qty:
                warns.append(f"BOM component '{cname}' has no quantity - skipped.")
                continue
            stock_uom = frappe.db.get_value("Item", ccode, "stock_uom")
            if uom and frappe.db.exists("UOM", uom):
                if uom != stock_uom:
                    warns.append(
                        f"component '{cname}' quantity is in '{uom}' but its stock "
                        f"unit is '{stock_uom}'; ERPNext assumes 1:1 unless the item "
                        "defines that conversion - verify the BOM quantity.")
            else:
                uom = stock_uom
            rows.append({"item_code": ccode, "qty": qty, "uom": uom})
        return rows, warns

    @staticmethod
    def _parse_qty_uom(raw):
        """' 1 Ream' -> (1.0, 'Ream'); '2 Box' -> (2.0, 'Box'); '' -> (0.0, '')."""
        raw = (raw or "").strip()
        if not raw:
            return 0.0, ""
        parts = raw.split(None, 1)
        try:
            return float(parts[0].replace(",", "")), (parts[1].strip() if len(parts) > 1 else "")
        except ValueError:
            return 0.0, ""
