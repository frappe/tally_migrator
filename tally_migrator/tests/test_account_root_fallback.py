"""Equity accounts must still find a root when the chart has no Equity root (#3b).

ERPNext's standard chart has no dedicated Equity root - proprietor/partner capital
and reserves nest on the Liabilities (funding) side. Without a fallback, an Equity
account that has to resolve to its root (a custom equity group under Primary, or the
reserved Capital Account group recreated in mirror mode) found no parent, failed to
import, and its opening balance silently diverted to Temporary Opening.

These mock the Account lookups so the logic is exercised deterministically, without
depending on any particular company's chart being present.
"""
import unittest
from unittest import mock

from tally_migrator.erpnext.importers.accounts import (
    AccountImporter, _ROOT_TYPE_FALLBACK,
)

LIAB_ROOT = "Source of Funds (Liabilities) - TC"
EQUITY_ROOT = "Equity - TC"


class TestEquityRootFallback(unittest.TestCase):
    def _imp(self):
        return AccountImporter("Test Co", "TC", mode="reuse")

    def _get_all(self, roots):
        """Fake frappe.get_all returning a no-parent root row for each root_type in
        ``roots`` (name -> row), and nothing for the rest."""
        def _fake(dt, fields=None, filters=None, **kw):
            name = roots.get(filters["root_type"])
            return [{"name": name, "parent_account": None}] if name else []
        return _fake

    def test_equity_falls_back_to_liability_when_no_equity_root(self):
        imp = self._imp()
        # No mapped base account exists; only a Liability root group is present.
        with mock.patch("frappe.db.exists", return_value=False), \
                mock.patch("frappe.get_all",
                           side_effect=self._get_all({"Liability": LIAB_ROOT})):
            self.assertEqual(imp._root_group("Equity"), LIAB_ROOT)

    def test_equity_root_used_directly_when_the_chart_ships_one(self):
        # A custom chart that does have an Equity root must use it, never the fallback.
        imp = self._imp()
        with mock.patch("frappe.db.exists", return_value=False), \
                mock.patch("frappe.get_all",
                           side_effect=self._get_all({"Equity": EQUITY_ROOT,
                                                      "Liability": LIAB_ROOT})):
            self.assertEqual(imp._root_group("Equity"), EQUITY_ROOT)

    def test_other_root_types_have_no_fallback_and_stay_none(self):
        # Only Equity falls back; a genuinely missing Income root must not be silently
        # rehomed onto some other root.
        imp = self._imp()
        with mock.patch("frappe.db.exists", return_value=False), \
                mock.patch("frappe.get_all", side_effect=self._get_all({})):
            self.assertIsNone(imp._root_group("Income"))
        self.assertEqual(_ROOT_TYPE_FALLBACK, {"Equity": "Liability"})

    def test_equity_returns_none_only_if_liability_also_absent(self):
        # Degenerate chart with neither root: no regression, still None (never raises).
        imp = self._imp()
        with mock.patch("frappe.db.exists", return_value=False), \
                mock.patch("frappe.get_all", side_effect=self._get_all({})):
            self.assertIsNone(imp._root_group("Equity"))


if __name__ == "__main__":
    unittest.main()
