"""Unit tests for MigrationSummary (no DB required)."""
import unittest
from unittest import mock

from tally_migrator.erpnext.importers import ImportResult
from tally_migrator.migration import master_migrator
from tally_migrator.migration.master_migrator import MasterMigrator, MigrationSummary


def _result(doctype, created=0, skipped=0, errors=(), created_names=(), warnings=()):
    r = ImportResult(doctype)
    r.created = created
    r.skipped = skipped
    for nm in created_names:
        r.created_names.append(nm)
        r.created_docs.append({"name": nm, "doctype": doctype})
    for name, reason in errors:
        r.add_error(name, reason)
    for name, reason in warnings:
        r.add_warning(name, reason)
    return r


def _summary(warehouse, customer, supplier, item):
    """Build a MigrationSummary with the standard four entities, in order."""
    return MigrationSummary({
        "Warehouses": warehouse,
        "Customers": customer,
        "Suppliers": supplier,
        "Items": item,
    })


class TestMigrationSummary(unittest.TestCase):
    def test_no_errors_is_false_and_empty_lines(self):
        s = _summary(_result("Warehouse", created=2), _result("Customer", created=3), _result("Supplier", skipped=1), _result("Item", created=5))
        self.assertFalse(s.has_errors)
        self.assertEqual(s.error_lines(), "")

    def test_error_lines_are_labelled_and_flattened(self):
        s = _summary(_result("Warehouse"), _result("Customer", errors=[("Acme", "Invalid GST")]), _result("Supplier"), _result("Item", errors=[("Widget", "bad UOM")]))
        self.assertTrue(s.has_errors)
        lines = s.error_lines().splitlines()
        self.assertIn("[Customers] Acme: Invalid GST", lines)
        self.assertIn("[Items] Widget: Bad UOM", lines)
        self.assertEqual(len(lines), 2)

    def test_as_dict_shape(self):
        s = _summary(_result("Warehouse", created=1), _result("Customer", created=2, errors=[("X", "e")]), _result("Supplier"), _result("Item"))
        d = s.as_dict()
        self.assertEqual(set(d), {"Warehouses", "Customers", "Suppliers", "Items"})
        self.assertEqual(d["Customers"]["created"], 2)
        self.assertEqual(d["Customers"]["failed"], 1)

    def test_error_records_are_structured_and_labelled(self):
        s = _summary(_result("Warehouse"), _result("Customer", errors=[("Acme", "Invalid GST")]), _result("Supplier"), _result("Item", errors=[("Widget", "bad UOM")]))
        records = s.error_records()
        self.assertEqual(len(records), 2)
        self.assertIn(
            {"status": "Failed", "record_type": "Customers", "record_name": "Acme", "reason": "Invalid GST"},
            records)
        self.assertIn(
            {"status": "Failed", "record_type": "Items", "record_name": "Widget", "reason": "Bad UOM"},
            records)

    def test_identical_reasons_collapse_to_one_row(self):
        # Three items, byte-identical reason -> one row listing all three, count
        # prefixed; a fourth item with a *different* reason stays its own row.
        same = "no HSN/SAC code - imported without one."
        s = _summary(
            _result("Warehouse"),
            _result("Customer"),
            _result("Supplier"),
            _result("Item", warnings=[("Pen", same), ("Pencil", same),
                                      ("Eraser", same), ("Glue", "bad UOM")]))
        records = s.error_records()
        collapsed = next(r for r in records if "No HSN" in r["reason"])
        self.assertIn("3 records", collapsed["reason"])
        self.assertEqual(collapsed["record_name"], "Pen, Pencil, Eraser")
        # Non-fatal drops are marked by the explicit Status column, not a record_type
        # suffix or a glyph baked into the reason.
        self.assertEqual(collapsed["status"], "Skipped")
        self.assertEqual(collapsed["record_type"], "Items")
        self.assertNotIn("⚠", collapsed["reason"])
        # The odd-one-out is untouched and not merged.
        self.assertTrue(any("Bad UOM" in r["reason"] for r in records))
        self.assertEqual(len(records), 2)

    def test_unique_reasons_pass_through_unchanged(self):
        # Reasons that embed the record name are naturally distinct -> no collapse.
        s = _summary(
            _result("Warehouse"),
            _result("Customer", errors=[("Acme", "Acme failed"), ("Beta", "Beta failed")]),
            _result("Supplier"),
            _result("Item"))
        records = s.error_records()
        self.assertEqual(len(records), 2)
        self.assertTrue(all("records ·" not in r["reason"] for r in records))

    def test_created_records_lists_only_nonempty_entities(self):
        s = _summary(
            _result("Warehouse", created=1, created_names=["Main - X"]),
            _result("Customer", created=2, created_names=["Acme", "Bolt"]),
            _result("Supplier"),                       # nothing created → omitted
            _result("Item", created=1, created_names=["WIDGET"]),
        )
        cr = s.created_records()
        self.assertEqual(set(cr), {"Warehouses", "Customers", "Items"})
        # Each entry is {name, doctype} so the log can deep-link it.
        self.assertEqual([d["name"] for d in cr["Customers"]], ["Acme", "Bolt"])
        self.assertTrue(all(d["doctype"] == "Customer" for d in cr["Customers"]))
        self.assertNotIn("Suppliers", cr)

    def test_add_created_increments_and_records_name(self):
        r = ImportResult("Journal Entry")
        r.add_created("ACC-JV-0001")
        self.assertEqual(r.created, 1)
        self.assertEqual(r.created_names, ["ACC-JV-0001"])
        self.assertEqual(r.created_docs, [{"name": "ACC-JV-0001", "doctype": "Journal Entry"}])

    def test_add_created_uses_explicit_doctype_when_given(self):
        # A heterogeneous importer (party openings, bank accounts) tags the real
        # doctype, not the result's label.
        r = ImportResult("Opening Invoice")
        r.add_created("ABC/1", "Sales Invoice")
        self.assertEqual(r.created_docs, [{"name": "ABC/1", "doctype": "Sales Invoice"}])

    def test_error_records_empty_when_clean(self):
        s = _summary(_result("Warehouse", created=1), _result("Customer", created=2), _result("Supplier"), _result("Item"))
        self.assertEqual(s.error_records(), [])


class TestTrackWizardUoms(unittest.TestCase):
    """The pre-flight 'create as new' UOMs are inserted before the run, so the unit
    importer skips them and never records them. _track_wizard_uoms folds them into the
    manifest so revert undoes them - but only the ones that actually exist, and never
    a duplicate of one the importer already recorded."""

    def _migrator(self, created_uoms):
        m = object.__new__(MasterMigrator)   # bypass __init__ (needs a live source)
        m.created_uoms = created_uoms
        return m

    def test_existing_wizard_uoms_added_without_duplicates(self):
        m = self._migrator(["Dozen", "Ream", "Ghost"])
        manifest = {"Units": [{"doctype": "UOM", "name": "Pcs"},
                              {"doctype": "UOM", "name": "Dozen"}]}  # Dozen already tracked
        # Ghost was reported created but no longer exists -> must be skipped.
        with mock.patch.object(master_migrator.frappe.db, "exists",
                               side_effect=lambda dt, n: n in {"Dozen", "Ream"}):
            m._track_wizard_uoms(manifest)
        names = [d["name"] for d in manifest["Units"]]
        self.assertEqual(names, ["Pcs", "Dozen", "Ream"])  # Ream added; no dup Dozen; no Ghost

    def test_creates_units_label_when_absent(self):
        m = self._migrator(["Ream"])
        manifest = {}   # run created no Unit masters of its own
        with mock.patch.object(master_migrator.frappe.db, "exists", return_value=True):
            m._track_wizard_uoms(manifest)
        self.assertEqual(manifest["Units"], [{"doctype": "UOM", "name": "Ream"}])

    def test_no_wizard_uoms_leaves_manifest_untouched(self):
        m = self._migrator([])
        manifest = {"Items": [{"doctype": "Item", "name": "X"}]}
        m._track_wizard_uoms(manifest)
        self.assertEqual(manifest, {"Items": [{"doctype": "Item", "name": "X"}]})


if __name__ == "__main__":
    unittest.main()
