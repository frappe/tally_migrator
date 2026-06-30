"""Pure unit tests for the shared Tally-name -> ERPNext-code transforms.

No frappe needed, so these run under ``make test-pure`` and ``make test-bench``.
"""
import unittest

from tally_migrator.naming import safe_item_code


class TestSafeItemCode(unittest.TestCase):
    def test_reserved_new_item_prefix_is_disambiguated(self):
        # Frappe reserves "New Item" (and any "New Item..." prefix) for the unsaved-
        # document placeholder and raises NameError on save, so the code must not
        # start with it.
        self.assertFalse(safe_item_code("New Item").startswith("New Item"))
        self.assertEqual(safe_item_code("New Item"), "Item - New Item")
        self.assertFalse(safe_item_code("New Item 2").startswith("New Item"))
        self.assertFalse(safe_item_code("New Items").startswith("New Item"))

    def test_disambiguation_is_idempotent(self):
        # Running an already-safe code back through must not double-prefix it, so a
        # re-run still matches the existing item.
        self.assertEqual(safe_item_code("Item - New Item"), "Item - New Item")

    def test_only_the_case_frappe_rejects_changes(self):
        # Frappe's reserved check is case-sensitive; other casings are valid names and
        # must be left untouched (changing them would needlessly alter item codes).
        self.assertEqual(safe_item_code("new item"), "new item")
        self.assertEqual(safe_item_code("NEW ITEM"), "NEW ITEM")

    def test_normal_codes_unchanged(self):
        self.assertEqual(safe_item_code("Widget"), "Widget")
        self.assertEqual(safe_item_code("A/B"), "A-B")
        self.assertEqual(safe_item_code("  Spaced  "), "Spaced")
        self.assertEqual(safe_item_code(""), "")
        self.assertEqual(safe_item_code(None), "")

    def test_result_never_exceeds_140_chars(self):
        self.assertEqual(len(safe_item_code("x" * 200)), 140)
        self.assertLessEqual(len(safe_item_code("New Item " + "y" * 200)), 140)


if __name__ == "__main__":
    unittest.main()
