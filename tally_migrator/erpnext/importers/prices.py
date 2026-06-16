"""Price importer: Tally price levels -> ERPNext Price List + Item Price (+ a
price-list-scoped Pricing Rule for any per-level discount)."""

import frappe

from tally_migrator.naming import safe_item_code
from .base import BaseImporter, ImportResult


class PriceImporter:
    """Create selling Price Lists and Item Prices from Tally price levels.

    Each Tally price level (Retail / Wholesale) becomes a selling Price List; the
    level's rate becomes an Item Price on that list. A per-level discount becomes a
    Pricing Rule scoped to that price list (for_price_list), so it applies only when
    that list is used - mirroring Tally's "rate then discount" billing. Runs after
    Items so the item_code/UOM links resolve. All upserts are idempotent.
    """

    doctype = "Item Price"

    def __init__(self, company: str, abbr: str):
        self.company = company
        self.abbr = abbr
        self.currency = frappe.get_cached_value(
            "Company", company, "default_currency") or "INR"

    def run(self, items: list[dict]) -> ImportResult:
        result = ImportResult(self.doctype)
        for it in items:
            for lv in (it.get("PriceLevels") or []):
                self._import_level(result, it.get("_name", ""), lv)
        return result

    def _import_level(self, result: ImportResult, item_name: str, lv: dict) -> None:
        rate, uom = self._parse_rate(lv.get("rate", ""))
        if rate is None:
            return                              # a level with no rate carries nothing
        item_code = safe_item_code(item_name)
        if not frappe.db.exists("Item", item_code):
            return                              # item didn't import (warned by ItemImporter)
        # The price-level UOM must be valid FOR THE ITEM - its stock UOM or one of its
        # additional UOMs - or ERPNext rejects the Item Price ("UOM X not found in
        # Item"). Fall back to the item's stock UOM (and warn) rather than hard-fail.
        stock_uom = frappe.db.get_value("Item", item_code, "stock_uom")
        if not self._uom_valid_for_item(item_code, uom, stock_uom):
            level = (lv.get("level") or "").strip()
            if uom and uom != stock_uom:
                result.add_warning(
                    f"{item_name} ({level})",
                    f"price-level unit '{uom}' is not a unit of item '{item_code}'; "
                    f"used its stock unit '{stock_uom}' instead - verify the price.")
            uom = stock_uom
        level = (lv.get("level") or "").strip()
        if not level:
            return
        try:
            price_list = self._ensure_price_list(level)
            valid_from = self._parse_date(lv.get("date"))
            self._ensure_item_price(result, item_code, price_list, uom, rate, valid_from)
            disc = BaseImporter._to_float(lv.get("discount"))
            if disc:
                self._ensure_pricing_rule(
                    result, item_code, price_list, disc, valid_from, lv.get("ending"))
        except Exception as exc:
            result.add_error(f"{item_name} ({level})", exc)
            frappe.db.rollback()

    @staticmethod
    def _uom_valid_for_item(item_code: str, uom: str, stock_uom: str) -> bool:
        """True when ``uom`` may be used on an Item Price for this item - its stock
        UOM or one of its additional UOMs (UOM Conversion Detail rows)."""
        if not uom:
            return False
        if uom == stock_uom:
            return True
        return bool(frappe.db.exists(
            "UOM Conversion Detail", {"parent": item_code, "uom": uom}))

    # ── upserts ────────────────────────────────────────────────────────────────
    def _ensure_price_list(self, level: str) -> str:
        if not frappe.db.exists("Price List", level):
            frappe.get_doc({
                "doctype": "Price List", "price_list_name": level,
                "selling": 1, "enabled": 1, "currency": self.currency,
            }).insert(ignore_permissions=True)
            frappe.db.commit()
        return level

    def _ensure_item_price(self, result, item_code, price_list, uom, rate, valid_from):
        if frappe.db.exists("Item Price", {
                "item_code": item_code, "price_list": price_list, "uom": uom}):
            result.skipped += 1
            return
        doc = frappe.get_doc({
            "doctype": "Item Price", "item_code": item_code, "uom": uom,
            "price_list": price_list, "price_list_rate": rate, "selling": 1,
            "currency": self.currency, "valid_from": valid_from or None,
        })
        doc.insert(ignore_permissions=True)
        frappe.db.commit()
        result.add_created(doc.name, "Item Price")

    def _ensure_pricing_rule(self, result, item_code, price_list, discount,
                             valid_from, ending):
        # Deterministic title is the idempotency key (Pricing Rule has no natural
        # one), so a re-run finds and skips the rule rather than duplicating it.
        title = f"Tally {price_list} discount - {item_code}"[:140]
        # Pricing Rule is autonamed by series (PRLE-####), so its document `name` is
        # NOT the title - the idempotency key must query the title FIELD, not name
        # (frappe.db.exists("Pricing Rule", title) tests name==title, never matches,
        # so a re-run would duplicate the rule).
        if frappe.db.exists("Pricing Rule", {"title": title}):
            return
        # for_price_list scopes the rule to this price list, so the discount applies
        # only when selling at that level, on its Item Price rate (Retail 398 - 10%
        # = 358.20) - mirroring Tally. NB: do NOT set apply_discount_on_rate: that is
        # the "stack on an already-discounted rate" flag and would force a Priority.
        rule = {
            "doctype": "Pricing Rule", "title": title,
            "apply_on": "Item Code", "items": [{"item_code": item_code}],
            "price_or_product_discount": "Price",
            "rate_or_discount": "Discount Percentage",
            "discount_percentage": discount,
            "for_price_list": price_list, "selling": 1, "company": self.company,
            "valid_from": valid_from or None,
        }
        # The slab's qty ceiling (ENDINGAT "100 Ream") bounds the discount, when present.
        max_qty = self._parse_qty(ending)
        if max_qty:
            rule["max_qty"] = max_qty
        # Log the autonamed document `name` (PRLE-####), not the title, so the
        # migration-log hyperlink resolves to a real Pricing Rule. Logging the title
        # produced a dead link (no doc is named after the title).
        doc = frappe.get_doc(rule)
        doc.insert(ignore_permissions=True)
        frappe.db.commit()
        result.add_created(doc.name, "Pricing Rule")

    # ── parsing ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _parse_rate(raw: str):
        """'398.00/Ream' -> (398.0, 'Ream'); '360.00' -> (360.0, ''); '' -> (None, '')."""
        raw = (raw or "").strip()
        if not raw:
            return None, ""
        rate_part, _, uom = raw.partition("/")
        try:
            return float(rate_part.replace(",", "").strip()), uom.strip()
        except ValueError:
            return None, ""

    @staticmethod
    def _parse_qty(raw: str) -> float:
        """' 100 Ream' -> 100.0; '' -> 0.0."""
        raw = (raw or "").strip()
        num = raw.split()[0] if raw else ""
        try:
            return float(num.replace(",", ""))
        except ValueError:
            return 0.0

    @staticmethod
    def _parse_date(raw: str):
        """Tally 'YYYYMMDD' -> 'YYYY-MM-DD'; anything else -> ''."""
        raw = (raw or "").strip()
        if len(raw) == 8 and raw.isdigit():
            return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
        return ""
