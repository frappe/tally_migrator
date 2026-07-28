"""BOM importer: Tally multi-component lists -> ERPNext BOM (submitted, active)."""

import frappe

from tally_migrator.naming import safe_item_code
from .base import BaseImporter, ImportResult

# Tally BOM "Type of Item" (NATUREOFITEM, normalised by _norm_nature) -> ERPNext
# BOM Secondary Item.type. Verified against a real TallyPrime export: the raw values are
# exactly "Component" / "Co-Product" / "By-Product" / "Scrap", and ERPNext's type options
# ("Co-Product" / "By-Product" / "Scrap" / "Additional Finished Good") match the first
# three verbatim. "Component" is not here - those rows are the BOM's raw materials.
_SECONDARY_TYPE = {"coproduct": "Co-Product", "byproduct": "By-Product", "scrap": "Scrap"}


class BomImporter:
    """Create ERPNext BOMs from Tally bills of materials.

    Each Tally BOM becomes a submitted, active BOM; the first BOM per item is the
    default. Runs after Items so the finished item and components exist. Tally's
    NATUREOFITEM classifies each row: "Component" rows become the BOM's raw materials
    (``items``), while "Co-Product" / "By-Product" / "Scrap" rows become ``secondary_items``
    carrying their cost-allocation percentage (Tally's ADDLCOSTALLOCPERC -> ERPNext's
    cost_allocation_per). ERPNext auto-reduces the finished good's own allocation so the
    total stays 100%. The type strings map verbatim (verified against a real TallyPrime
    export); an unrecognised type is skipped with a warning.

    Idempotent by skip-if-exists: BOM names auto-increment, so a re-run would
    create duplicates; if any BOM already exists for an item we skip it entirely.
    """

    doctype = "BOM"

    def __init__(self, company: str, abbr: str):
        self.company = company
        self.abbr = abbr
        self.currency = frappe.get_cached_value(
            "Company", company, "default_currency") or "INR"

    def run(self, items: list[dict], on_progress=None) -> ImportResult:
        result = ImportResult(self.doctype)
        total = len(items)
        for idx, it in enumerate(items, 1):
            if on_progress:
                on_progress(idx, total)
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
        rows, secondary_rows, warns = self._classify_rows(code, bom.get("components") or [])
        for w in warns:
            result.add_warning(item_name, w)
        if not rows:
            # A BOM needs at least one raw material; a Tally BOM with only secondaries
            # (or no usable component) can't post, so skip it (its warnings are recorded).
            result.add_warning(item_name, "BOM skipped - no usable Component rows.")
            return
        if secondary_rows:
            # ERPNext derives the finished good's own cost allocation as 100% minus the
            # secondaries' total, and rejects the whole BOM if that goes negative ("% Cost
            # Allocation cannot be negative" - verified live). Tally's per-row Rate (%) are
            # independent recoveries and are NOT constrained to sum to <=100, so a real book
            # can exceed it. Rather than lose the entire BOM (components included), drop the
            # secondaries, keep the BOM, and say so - scaling the percentages would silently
            # misrepresent the source figures. Exactly 100% is allowed (finished good = 0),
            # so only a genuine overage trips this (epsilon guards float noise).
            total_alloc = sum(s["cost_allocation_per"] for s in secondary_rows)
            if total_alloc > 100 + 1e-6:
                result.add_warning(
                    item_name,
                    f"BOM {bom.get('name')}: its co-product/by-product/scrap cost "
                    f"allocations add up to {total_alloc:g}%, over 100%, which ERPNext will "
                    "not accept - the BOM was imported with its components only. Add the "
                    "secondary items manually in ERPNext if you need them.")
                secondary_rows = []
        qty, uom = self._parse_qty_uom(bom.get("basic_qty"))
        doc = {
            "doctype": "BOM", "item": code, "company": self.company,
            "quantity": qty or 1, "currency": self.currency, "conversion_rate": 1,
            "is_active": 1, "is_default": 1 if is_default else 0, "items": rows,
        }
        if secondary_rows:
            # Co-Product / By-Product / Scrap. Only type/item_code/qty/cost_allocation_per
            # are supplied; ERPNext fills uom, conversion_factor, and the derived costs, and
            # reduces the finished good's cost_allocation_per so the total is 100% (verified
            # live against ERPNext's BOM.set_fg_cost_allocation / validate_total_cost_allocation).
            doc["secondary_items"] = secondary_rows
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

    def _classify_rows(self, finished_code, components):
        """Split a Tally BOM's rows into ERPNext raw materials (``items``) and secondary
        outputs (``secondary_items``) by NATUREOFITEM. Returns (rows, secondary_rows, warns).

        Shares the same per-row guards for both (item must exist, not be the finished item
        itself, and carry a quantity). Secondary rows additionally carry the cost-allocation
        percentage; ERPNext derives their uom/conversion/cost, so only type/item_code/qty/
        cost_allocation_per are supplied (verified live)."""
        rows, secondary_rows, warns = [], [], []
        for c in components:
            nature = (c.get("natureofitem") or "Component").strip()
            cname = (c.get("stockitemname") or "").strip()
            norm = self._norm_nature(nature)
            sec_type = _SECONDARY_TYPE.get(norm)
            if norm != "component" and not sec_type:
                warns.append(f"BOM row '{cname}' has an unrecognised type '{nature}' "
                             "- skipped.")
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
            if sec_type:
                secondary_rows.append({
                    "type": sec_type, "item_code": ccode, "qty": qty,
                    "cost_allocation_per": self._parse_percent(c.get("addlcostallocperc")),
                })
            else:
                rows.append({"item_code": ccode, "qty": qty, "uom": uom})
        return rows, secondary_rows, warns

    @staticmethod
    def _norm_nature(nature: str) -> str:
        """Normalise a Tally NATUREOFITEM for lookup: lowercase, drop spaces/hyphens/
        underscores. 'Co-Product' -> 'coproduct', 'By-Product' -> 'byproduct'."""
        return "".join((nature or "").lower().split()).replace("-", "").replace("_", "")

    @staticmethod
    def _parse_percent(raw) -> float:
        """'20' / ' 20 ' / '20%' -> 20.0; blank/garbage -> 0.0 (a valid allocation that
        ERPNext accepts - the finished good simply keeps that share)."""
        raw = (raw or "").strip()
        if not raw:
            return 0.0
        try:
            return float(raw.replace(",", "").replace("%", "").strip())
        except ValueError:
            return 0.0

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
