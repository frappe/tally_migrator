"""
Phase 4 tests: Tally price levels -> Price List + Item Price + (discount) Pricing Rule.

Per price level the latest dated revision wins; the rate becomes an Item Price on
a selling Price List named after the level, and any per-level discount becomes a
Pricing Rule scoped to that price list (for_price_list) so it mirrors Tally's
"rate then discount" billing.
"""
import types
import unittest
from unittest import mock

from tally_migrator.tally.file_source import FileTallySource
from tally_migrator.erpnext.importers.prices import PriceImporter
from tally_migrator.erpnext.importers import ImportResult


class TestPriceLevelExtraction(unittest.TestCase):
    def test_latest_date_wins_and_slabs_parsed(self):
        xml = (
            '<ENVELOPE><BODY><IMPORTDATA><REQUESTDATA><TALLYMESSAGE>'
            '<STOCKITEM NAME="A4"><NAME>A4</NAME>'
            '<FULLPRICELIST.LIST><DATE>20250401</DATE><PRICELEVEL>Retail</PRICELEVEL>'
            '<PRICELEVELLIST.LIST><ENDINGAT> 100 Ream</ENDINGAT><RATE>398.00/Ream</RATE>'
            '<DISCOUNT> 10</DISCOUNT></PRICELEVELLIST.LIST></FULLPRICELIST.LIST>'
            '<FULLPRICELIST.LIST><DATE>20260401</DATE><PRICELEVEL>Retail</PRICELEVEL>'
            '<PRICELEVELLIST.LIST><RATE>420.00/Ream</RATE><DISCOUNT> 12</DISCOUNT>'
            '</PRICELEVELLIST.LIST></FULLPRICELIST.LIST>'
            '<FULLPRICELIST.LIST><DATE>20260401</DATE><PRICELEVEL>Wholesale</PRICELEVEL>'
            '<PRICELEVELLIST.LIST><RATE>360.00/Ream</RATE><DISCOUNT> 15</DISCOUNT>'
            '</PRICELEVELLIST.LIST></FULLPRICELIST.LIST>'
            '</STOCKITEM></TALLYMESSAGE></REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>'
        )
        levels = {l["level"]: l for l in FileTallySource(xml).item_price_levels()["A4"]}
        self.assertEqual(set(levels), {"Retail", "Wholesale"})
        self.assertEqual(levels["Retail"]["rate"], "420.00/Ream")     # latest date, not 398
        self.assertEqual(levels["Retail"]["discount"], "12")
        self.assertEqual(levels["Wholesale"]["rate"], "360.00/Ream")


class TestParsers(unittest.TestCase):
    def test_parse_rate(self):
        self.assertEqual(PriceImporter._parse_rate("398.00/Ream"), (398.0, "Ream"))
        self.assertEqual(PriceImporter._parse_rate("360.00"), (360.0, ""))
        self.assertEqual(PriceImporter._parse_rate(""), (None, ""))
        self.assertEqual(PriceImporter._parse_rate("x/Ream"), (None, ""))
        # The price-level unit is normalised through UOM_MAP (like the item's stock
        # UOM), so "Pcs" resolves to "Nos" and matches the item instead of warning.
        self.assertEqual(PriceImporter._parse_rate("50.00/Pcs"), (50.0, "Nos"))

    def test_parse_qty_and_date(self):
        self.assertEqual(PriceImporter._parse_qty(" 100 Ream"), 100.0)
        self.assertEqual(PriceImporter._parse_qty(""), 0.0)
        self.assertEqual(PriceImporter._parse_date("20260401"), "2026-04-01")
        self.assertEqual(PriceImporter._parse_date("bad"), "")


class TestPriceImporter(unittest.TestCase):
    def _imp(self):
        with mock.patch("frappe.get_cached_value", return_value="INR"):
            return PriceImporter("Frappe Tech", "FT")

    def test_creates_price_list_item_price_and_discount_rule(self):
        imp = self._imp()
        res = ImportResult("Item Price")
        created = []

        def fake_get_doc(d):
            created.append(d)
            # A Pricing Rule is autonamed by series (PRLE-####), so its `name` is NOT
            # its title - mimic that here so the test would catch logging the title as
            # the (dead) hyperlink target, or keying idempotency on name==title.
            name = ("PRLE-0001" if d["doctype"] == "Pricing Rule"
                    else d.get("price_list_name") or "8mc65uqma1")
            return types.SimpleNamespace(name=name, insert=lambda **k: None)

        exists_state = {"Price List": False, "Item Price": False, "Pricing Rule": False}
        pricing_rule_filters = []

        def fake_exists(dt, filt=None):
            if dt == "Item":
                return True
            if dt == "UOM":
                return True
            if dt == "Pricing Rule":
                pricing_rule_filters.append(filt)
            return exists_state.get(dt, False)

        with mock.patch("frappe.db.exists", side_effect=fake_exists), \
                mock.patch("frappe.get_doc", side_effect=fake_get_doc), \
                mock.patch("frappe.db.commit"), \
                mock.patch("frappe.db.get_value", return_value="Ream"):
            imp._import_level(res, "A4 Paper Ream", {
                "level": "Retail", "date": "20260401",
                "rate": "398.00/Ream", "discount": "10", "ending": " 100 Ream"})

        kinds = [d["doctype"] for d in created]
        self.assertEqual(kinds, ["Price List", "Item Price", "Pricing Rule"])
        ip = next(d for d in created if d["doctype"] == "Item Price")
        self.assertEqual((ip["price_list"], ip["price_list_rate"], ip["uom"], ip["selling"]),
                         ("Retail", 398.0, "Ream", 1))
        self.assertEqual(ip["valid_from"], "2026-04-01")
        pr = next(d for d in created if d["doctype"] == "Pricing Rule")
        self.assertEqual(pr["for_price_list"], "Retail")
        self.assertEqual(pr["rate_or_discount"], "Discount Percentage")
        self.assertEqual(pr["discount_percentage"], 10.0)
        self.assertNotIn("apply_discount_on_rate", pr)       # would force a Priority
        self.assertEqual(pr["max_qty"], 100.0)               # ENDINGAT bound carried
        self.assertEqual(pr["items"], [{"item_code": "A4 Paper Ream"}])
        # Idempotency must key on the title FIELD, not name (Pricing Rule autonames
        # by series, so name != title; a bare-title exists() never matches -> dupes).
        self.assertEqual(pricing_rule_filters, [{"title": "Tally Retail discount - A4 Paper Ream"}])
        # The logged hyperlink target is the real autonamed doc, not the title, so
        # the migration-log link resolves (the title is no document's name).
        self.assertIn("PRLE-0001", res.created_names)
        self.assertNotIn("Tally Retail discount - A4 Paper Ream", res.created_names)

    def test_idempotent_skips_existing(self):
        imp = self._imp()
        res = ImportResult("Item Price")
        created = []
        with mock.patch("frappe.db.exists", return_value=True), \
                mock.patch("frappe.get_doc", side_effect=lambda d: created.append(d)), \
                mock.patch("frappe.db.commit"), \
                mock.patch("frappe.db.get_value", return_value="Ream"):
            imp._import_level(res, "A4 Paper Ream", {
                "level": "Retail", "date": "20260401",
                "rate": "398.00/Ream", "discount": "10", "ending": ""})
        # Price List exists (reused), Item Price exists (skipped), Pricing Rule exists (skipped)
        self.assertEqual(created, [])
        self.assertEqual(res.skipped, 1)

    def test_falls_back_to_stock_uom_when_price_uom_invalid_for_item(self):
        imp = self._imp()
        res = ImportResult("Item Price")
        created = []

        def fake_get_doc(d):
            created.append(d)
            return types.SimpleNamespace(name="IP", insert=lambda **k: None)

        def fake_exists(dt, filt=None):
            if dt == "Item":
                return True
            if dt == "UOM Conversion Detail":
                return False          # 'Ream' is NOT an additional uom of the item
            if dt in ("Price List", "Item Price"):
                return False
            return False
        with mock.patch("frappe.db.exists", side_effect=fake_exists), \
                mock.patch("frappe.db.get_value", return_value="Nos"), \
                mock.patch("frappe.get_doc", side_effect=fake_get_doc), \
                mock.patch("frappe.db.commit"):
            imp._import_level(res, "A4 Paper Ream", {
                "level": "Retail", "date": "20260401",
                "rate": "398.00/Ream", "discount": "", "ending": ""})

        ip = next(d for d in created if d["doctype"] == "Item Price")
        self.assertEqual(ip["uom"], "Nos")               # fell back to stock uom, no hard fail
        self.assertTrue(any("not a unit of item" in w["reason"] for w in res.warnings))

    def test_no_rate_creates_nothing(self):
        imp = self._imp()
        res = ImportResult("Item Price")
        with mock.patch("frappe.get_doc") as gd:
            imp._import_level(res, "X", {"level": "Retail", "rate": "", "discount": "5"})
        gd.assert_not_called()


if __name__ == "__main__":
    unittest.main()
