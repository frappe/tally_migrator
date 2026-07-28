"""
Phase 5 tests: Tally multi-component lists -> ERPNext BOM.

Covers the two guards live re-validation proved necessary: a component UOM that
differs from the item's stock UOM is kept but WARNED (ERPNext silently assumes
1:1, so it needs review), and a self-referential component is explicitly skipped
(ERPNext does not reject it). Plus skip-if-exists idempotency, and the Co-Product /
By-Product / Scrap mapping to ERPNext secondary_items (structure verified against a
real TallyPrime export; the ERPNext side verified live).
"""
import types
import unittest
from unittest import mock

from tally_migrator.tally.file_source import FileTallySource
from tally_migrator.erpnext.importers.bom import BomImporter
from tally_migrator.erpnext.importers import ImportResult


class TestBomExtraction(unittest.TestCase):
    def test_reads_multicomponent_ignores_empty_legacy_list(self):
        # Component + a Scrap secondary carrying ADDLCOSTALLOCPERC, in the exact tag shape
        # of a real TallyPrime export (a single MULTICOMPONENTITEMLIST.LIST per row).
        xml = (
            '<ENVELOPE><BODY><IMPORTDATA><REQUESTDATA><TALLYMESSAGE>'
            '<STOCKITEM NAME="Kit"><NAME>Kit</NAME>'
            '<COMPONENTLIST.LIST>      </COMPONENTLIST.LIST>'        # legacy empty - ignored
            '<MULTICOMPONENTLIST.LIST><COMPONENTLISTNAME>Kit</COMPONENTLISTNAME>'
            '<COMPONENTBASICQTY> 1 Nos</COMPONENTBASICQTY>'
            '<MULTICOMPONENTITEMLIST.LIST><NATUREOFITEM>Component</NATUREOFITEM>'
            '<STOCKITEMNAME>A4 Paper Ream</STOCKITEMNAME><GODOWNNAME>Main</GODOWNNAME>'
            '<ACTUALQTY> 1 Ream</ACTUALQTY></MULTICOMPONENTITEMLIST.LIST>'
            '<MULTICOMPONENTITEMLIST.LIST><NATUREOFITEM>Scrap</NATUREOFITEM>'
            '<STOCKITEMNAME>Offcut</STOCKITEMNAME><GODOWNNAME>Main</GODOWNNAME>'
            '<ADDLCOSTALLOCPERC> 2</ADDLCOSTALLOCPERC>'
            '<ACTUALQTY> 1 Nos</ACTUALQTY></MULTICOMPONENTITEMLIST.LIST>'
            '</MULTICOMPONENTLIST.LIST></STOCKITEM>'
            '</TALLYMESSAGE></REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>'
        )
        boms = FileTallySource(xml).item_boms()["Kit"]
        self.assertEqual(len(boms), 1)
        self.assertEqual(boms[0]["basic_qty"], "1 Nos")
        comps = boms[0]["components"]
        self.assertEqual(comps[0]["stockitemname"], "A4 Paper Ream")
        self.assertEqual(comps[0]["actualqty"], "1 Ream")
        # The Scrap row's cost-allocation percentage is now captured.
        self.assertEqual(comps[1]["natureofitem"], "Scrap")
        self.assertEqual(comps[1]["addlcostallocperc"], "2")


class TestParseQtyUom(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(BomImporter._parse_qty_uom(" 1 Ream"), (1.0, "Ream"))
        self.assertEqual(BomImporter._parse_qty_uom("2 Box"), (2.0, "Box"))
        self.assertEqual(BomImporter._parse_qty_uom("5"), (5.0, ""))
        self.assertEqual(BomImporter._parse_qty_uom(""), (0.0, ""))


class TestParsePercent(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(BomImporter._parse_percent(" 20"), 20.0)
        self.assertEqual(BomImporter._parse_percent("5%"), 5.0)
        self.assertEqual(BomImporter._parse_percent(""), 0.0)
        self.assertEqual(BomImporter._parse_percent("junk"), 0.0)


class TestNormNature(unittest.TestCase):
    def test_norm(self):
        self.assertEqual(BomImporter._norm_nature("Co-Product"), "coproduct")
        self.assertEqual(BomImporter._norm_nature("By-Product"), "byproduct")
        self.assertEqual(BomImporter._norm_nature(" Scrap "), "scrap")
        self.assertEqual(BomImporter._norm_nature("Component"), "component")


class TestClassifyRows(unittest.TestCase):
    def _imp(self):
        with mock.patch("frappe.get_cached_value", return_value="INR"):
            return BomImporter("Frappe Tech", "FT")

    def test_components_and_secondaries_split_with_guards(self):
        imp = self._imp()
        comps = [
            {"natureofitem": "Component",  "stockitemname": "A", "actualqty": "2 Box"},
            {"natureofitem": "Co-Product", "stockitemname": "CO", "actualqty": "1 Nos",
             "addlcostallocperc": " 20"},
            {"natureofitem": "By-Product", "stockitemname": "BP", "actualqty": "1 Nos",
             "addlcostallocperc": " 5"},
            {"natureofitem": "Scrap",      "stockitemname": "SC", "actualqty": "1 Nos",
             "addlcostallocperc": " 2"},
            {"natureofitem": "Mystery",    "stockitemname": "MY", "actualqty": "1 Nos"},  # unknown
            {"natureofitem": "Component",  "stockitemname": "Missing", "actualqty": "1 Nos"},
            {"natureofitem": "Component",  "stockitemname": "Kit", "actualqty": "1 Nos"},   # self
            {"natureofitem": "Component",  "stockitemname": "C", "actualqty": "1 Nos"},     # uom mismatch
            {"natureofitem": "Component",  "stockitemname": "D", "actualqty": ""},          # no qty
        ]
        existing = {"A", "CO", "BP", "SC", "MY", "C", "Kit", "D"}
        stock = {"A": "Box", "CO": "Nos", "BP": "Nos", "SC": "Nos", "MY": "Nos",
                 "C": "Box", "Kit": "Nos", "D": "Nos"}

        def fake_exists(dt, name=None):
            if dt == "Item":
                return name in existing
            if dt == "UOM":
                return True
            return False
        with mock.patch("frappe.db.exists", side_effect=fake_exists), \
                mock.patch("frappe.db.get_value", side_effect=lambda d, n, f: stock.get(n)):
            rows, secondary_rows, warns = imp._classify_rows("Kit", comps)

        # Components -> items (uom resolved; the mismatch one kept + warned).
        self.assertEqual(rows, [
            {"item_code": "A", "qty": 2.0, "uom": "Box"},
            {"item_code": "C", "qty": 1.0, "uom": "Nos"},     # kept, but warned
        ])
        # Co-/By-/Scrap -> secondary_items with type + cost_allocation_per (no uom: ERPNext
        # derives it), verbatim ERPNext type names.
        self.assertEqual(secondary_rows, [
            {"type": "Co-Product", "item_code": "CO", "qty": 1.0, "cost_allocation_per": 20.0},
            {"type": "By-Product", "item_code": "BP", "qty": 1.0, "cost_allocation_per": 5.0},
            {"type": "Scrap",      "item_code": "SC", "qty": 1.0, "cost_allocation_per": 2.0},
        ])
        joined = " ".join(warns)
        self.assertIn("unrecognised type 'Mystery'", joined)  # unknown nature skipped
        self.assertIn("Missing", joined)             # missing item skipped
        self.assertIn("self-reference", joined)      # self skipped
        self.assertIn("stock unit is 'Box'", joined)  # C uom mismatch warned
        self.assertIn("no quantity", joined)         # D skipped


class TestBomImporterRun(unittest.TestCase):
    def _imp(self):
        with mock.patch("frappe.get_cached_value", return_value="INR"):
            return BomImporter("Frappe Tech", "FT")

    def _items(self):
        return [{"_name": "Kit", "Boms": [{"name": "Kit BOM", "basic_qty": "1 Nos",
                 "components": [{"natureofitem": "Component", "stockitemname": "A",
                                 "actualqty": "2 Nos"}]}]}]

    def test_creates_submitted_default_bom(self):
        imp = self._imp()
        captured = {}

        def fake_get_doc(d):
            captured["doc"] = d
            return types.SimpleNamespace(name="BOM-Kit-001",
                                         insert=lambda **k: None, submit=lambda: None)

        def fake_exists(dt, filt=None):
            if dt == "Item":
                return True
            if dt == "BOM":
                return False         # none yet -> create
            if dt == "UOM":
                return True
            return False
        with mock.patch("frappe.db.exists", side_effect=fake_exists), \
                mock.patch("frappe.db.get_value", return_value="Nos"), \
                mock.patch("frappe.get_doc", side_effect=fake_get_doc), \
                mock.patch("frappe.db.commit"):
            res = imp.run(self._items())

        self.assertEqual(res.created, 1)
        d = captured["doc"]
        self.assertEqual((d["item"], d["is_active"], d["is_default"], d["quantity"]),
                         ("Kit", 1, 1, 1.0))
        self.assertEqual(d["items"], [{"item_code": "A", "qty": 2.0, "uom": "Nos"}])

    def test_idempotent_skip_when_bom_exists(self):
        imp = self._imp()

        def fake_exists(dt, filt=None):
            return True              # Item + BOM both exist
        with mock.patch("frappe.db.exists", side_effect=fake_exists), \
                mock.patch("frappe.get_doc") as gd, \
                mock.patch("frappe.db.commit"):
            res = imp.run(self._items())
        gd.assert_not_called()
        self.assertEqual(res.skipped, 1)


if __name__ == "__main__":
    unittest.main()
