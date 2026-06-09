"""Tests for UomResolver - pure logic, no Frappe. Runnable locally:
    python -m unittest tally_migrator.tests.test_uom_resolver
"""
import unittest

from tally_migrator.erpnext.uom_resolver import UomResolver


class TestUomResolver(unittest.TestCase):
    def test_maps_and_flags_existing_vs_missing(self):
        r = UomResolver(["Nos", "Kg", "Metre"])
        issues = {i["tally_uom"]: i for i in r.issues_for(["Mtr", "Kg", "Bag"])}
        # "Mtr" maps to "Metre" which exists.
        self.assertEqual(issues["Mtr"]["erpnext_uom"], "Metre")
        self.assertTrue(issues["Mtr"]["exists"])
        # "Kg" exists as-is.
        self.assertTrue(issues["Kg"]["exists"])
        # "Bag" has no mapping and doesn't exist.
        self.assertEqual(issues["Bag"]["erpnext_uom"], "Bag")
        self.assertFalse(issues["Bag"]["exists"])

    def test_unknown_unit_falls_back_to_tally_name(self):
        r = UomResolver([])
        issues = r.issues_for(["Widget"])
        self.assertEqual(issues[0]["erpnext_uom"], "Widget")
        self.assertFalse(issues[0]["exists"])

    def test_dedups_blanks_and_sorts(self):
        r = UomResolver(["Nos"])
        names = [i["tally_uom"] for i in r.issues_for(["Bag", "", "  ", "Bag", "Kg"])]
        self.assertEqual(names, ["Bag", "Kg"])  # unique, blanks dropped, sorted

    def test_existing_sorted_is_clean(self):
        r = UomResolver([" Nos ", "Kg", "", "Nos"])
        self.assertEqual(r.existing_sorted, ["Kg", "Nos"])  # trimmed, deduped, sorted


if __name__ == "__main__":
    unittest.main()
