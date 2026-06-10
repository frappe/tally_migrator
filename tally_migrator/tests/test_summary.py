"""Unit tests for MigrationSummary (no DB required)."""
import unittest

from tally_migrator.erpnext.importers import ImportResult
from tally_migrator.migration.master_migrator import MigrationSummary


def _result(doctype, created=0, skipped=0, errors=(), created_names=(), warnings=()):
    r = ImportResult(doctype)
    r.created = created
    r.skipped = skipped
    r.created_names = list(created_names)
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
        self.assertIn("[Items] Widget: bad UOM", lines)
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
            {"record_type": "Customers", "record_name": "Acme", "reason": "Invalid GST"},
            records)
        self.assertIn(
            {"record_type": "Items", "record_name": "Widget", "reason": "bad UOM"},
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
        collapsed = next(r for r in records if "no HSN" in r["reason"])
        self.assertIn("3 records", collapsed["reason"])
        self.assertEqual(collapsed["record_name"], "Pen, Pencil, Eraser")
        self.assertTrue(collapsed["reason"].startswith("⚠ "))  # warning prefix kept
        # The odd-one-out is untouched and not merged.
        self.assertTrue(any("bad UOM" in r["reason"] for r in records))
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
        self.assertEqual(cr["Customers"], ["Acme", "Bolt"])
        self.assertNotIn("Suppliers", cr)

    def test_add_created_increments_and_records_name(self):
        r = ImportResult("Journal Entry")
        r.add_created("ACC-JV-0001")
        self.assertEqual(r.created, 1)
        self.assertEqual(r.created_names, ["ACC-JV-0001"])

    def test_error_records_empty_when_clean(self):
        s = _summary(_result("Warehouse", created=1), _result("Customer", created=2), _result("Supplier"), _result("Item"))
        self.assertEqual(s.error_records(), [])


if __name__ == "__main__":
    unittest.main()
