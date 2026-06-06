"""Unit tests for MigrationSummary (no DB required)."""
import unittest

from tally_migrator.erpnext.importers import ImportResult
from tally_migrator.migration.master_migrator import MigrationSummary


def _result(doctype, created=0, skipped=0, errors=()):
    r = ImportResult(doctype)
    r.created = created
    r.skipped = skipped
    for name, reason in errors:
        r.add_error(name, reason)
    return r


class TestMigrationSummary(unittest.TestCase):
    def test_no_errors_is_false_and_empty_lines(self):
        s = MigrationSummary(
            warehouses=_result("Warehouse", created=2),
            customers=_result("Customer", created=3),
            suppliers=_result("Supplier", skipped=1),
            items=_result("Item", created=5),
        )
        self.assertFalse(s.has_errors)
        self.assertEqual(s.error_lines(), "")

    def test_error_lines_are_labelled_and_flattened(self):
        s = MigrationSummary(
            warehouses=_result("Warehouse"),
            customers=_result("Customer", errors=[("Acme", "Invalid GST")]),
            suppliers=_result("Supplier"),
            items=_result("Item", errors=[("Widget", "bad UOM")]),
        )
        self.assertTrue(s.has_errors)
        lines = s.error_lines().splitlines()
        self.assertIn("[Customers] Acme: Invalid GST", lines)
        self.assertIn("[Items] Widget: bad UOM", lines)
        self.assertEqual(len(lines), 2)

    def test_as_dict_shape(self):
        s = MigrationSummary(
            warehouses=_result("Warehouse", created=1),
            customers=_result("Customer", created=2, errors=[("X", "e")]),
            suppliers=_result("Supplier"),
            items=_result("Item"),
        )
        d = s.as_dict()
        self.assertEqual(set(d), {"Warehouses", "Customers", "Suppliers", "Items"})
        self.assertEqual(d["Customers"]["created"], 2)
        self.assertEqual(d["Customers"]["failed"], 1)


if __name__ == "__main__":
    unittest.main()
