"""
Phase 5 tests: Tally multi-component lists -> ERPNext BOM.

Covers the two guards live re-validation proved necessary: a component UOM that
differs from the item's stock UOM is kept but WARNED (ERPNext silently assumes
1:1, so it needs review), and a self-referential component is explicitly skipped
(ERPNext does not reject it). Plus skip-if-exists idempotency and skip+warn for
non-Component natures.
"""
import types
import unittest
from unittest import mock

from tally_migrator.tally.file_source import FileTallySource
from tally_migrator.erpnext.importers.bom import BomImporter
from tally_migrator.erpnext.importers import ImportResult


class TestBomExtraction(unittest.TestCase):
    def test_reads_multicomponent_ignores_empty_legacy_list(self):
        xml = (
            '<ENVELOPE><BODY><IMPORTDATA><REQUESTDATA><TALLYMESSAGE>'
            '<STOCKITEM NAME="Kit"><NAME>Kit</NAME>'
            '<COMPONENTLIST.LIST>      </COMPONENTLIST.LIST>'        # legacy empty - ignored
            '<MULTICOMPONENTLIST.LIST><COMPONENTLISTNAME>Kit</COMPONENTLISTNAME>'
            '<COMPONENTBASICQTY> 1 Nos</COMPONENTBASICQTY>'
            '<MULTICOMPONENTITEMLIST.LIST><NATUREOFITEM>Component</NATUREOFITEM>'
            '<STOCKITEMNAME>A4 Paper Ream</STOCKITEMNAME><GODOWNNAME>Main</GODOWNNAME>'
            '<ACTUALQTY> 1 Ream</ACTUALQTY></MULTICOMPONENTITEMLIST.LIST>'
            '</MULTICOMPONENTLIST.LIST></STOCKITEM>'
            '</TALLYMESSAGE></REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>'
        )
        boms = FileTallySource(xml).item_boms()["Kit"]
        self.assertEqual(len(boms), 1)
        self.assertEqual(boms[0]["basic_qty"], "1 Nos")
        self.assertEqual(boms[0]["components"][0]["stockitemname"], "A4 Paper Ream")
        self.assertEqual(boms[0]["components"][0]["actualqty"], "1 Ream")


class TestParseQtyUom(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(BomImporter._parse_qty_uom(" 1 Ream"), (1.0, "Ream"))
        self.assertEqual(BomImporter._parse_qty_uom("2 Box"), (2.0, "Box"))
        self.assertEqual(BomImporter._parse_qty_uom("5"), (5.0, ""))
        self.assertEqual(BomImporter._parse_qty_uom(""), (0.0, ""))


class TestComponentRows(unittest.TestCase):
    def _imp(self):
        with mock.patch("frappe.get_cached_value", return_value="INR"):
            return BomImporter("Frappe Tech", "FT")

    def test_guards(self):
        imp = self._imp()
        comps = [
            {"natureofitem": "Component",  "stockitemname": "A", "actualqty": "2 Box"},
            {"natureofitem": "By-Product", "stockitemname": "BP", "actualqty": "1 Nos"},
            {"natureofitem": "Component",  "stockitemname": "Missing", "actualqty": "1 Nos"},
            {"natureofitem": "Component",  "stockitemname": "Kit", "actualqty": "1 Nos"},   # self
            {"natureofitem": "Component",  "stockitemname": "C", "actualqty": "1 Nos"},     # uom mismatch
            {"natureofitem": "Component",  "stockitemname": "D", "actualqty": ""},          # no qty
        ]
        existing = {"A", "C", "Kit", "D"}
        stock = {"A": "Box", "C": "Box", "Kit": "Nos", "D": "Nos"}

        def fake_exists(dt, name=None):
            if dt == "Item":
                return name in existing
            if dt == "UOM":
                return True
            return False
        with mock.patch("frappe.db.exists", side_effect=fake_exists), \
                mock.patch("frappe.db.get_value", side_effect=lambda d, n, f: stock.get(n)):
            rows, warns = imp._component_rows("Kit", comps)

        self.assertEqual(rows, [
            {"item_code": "A", "qty": 2.0, "uom": "Box"},
            {"item_code": "C", "qty": 1.0, "uom": "Nos"},     # kept, but warned
        ])
        joined = " ".join(warns)
        self.assertIn("By-Product", joined)          # non-Component skipped
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
