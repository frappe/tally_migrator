"""
Regression tests for the Phase-1 critical fixes. Run via ``bench run-tests``
(they import ``tally_migrator.erpnext.importers``, which imports ``frappe``).

Covered:
- C2: opening balances skip — not fail — when a referenced account/party is missing.
- C3: hierarchy sorters break circular parents instead of recursing forever.

C1 (override persistence on re-run) and C4 (company-scoped warehouse idempotency)
are exercised by the live-DB integration tests in ``test_importer`` / manual re-run,
since they depend on inserted documents.
"""
import types
import unittest
from unittest import mock

from tally_migrator.erpnext.importers import (
    OpeningBalanceImporter,
    WarehouseImporter,
    StockGroupImporter,
    AccountImporter,
    CostCentreImporter,
    ImportResult,
)


def _node(name, parent):
    """A COA/cost-centre node as the sorters see it (attribute access)."""
    return types.SimpleNamespace(name=name, parent=parent)


class TestHierarchyCycleSafety(unittest.TestCase):
    """C3: a circular parent relationship must not blow the stack."""

    def test_warehouse_topo_sort_breaks_cycle(self):
        data = [{"_name": "A", "Parent": "B"}, {"_name": "B", "Parent": "A"}]
        out = WarehouseImporter._topo_sort(data)
        self.assertEqual({w["_name"] for w in out}, {"A", "B"})

    def test_stock_group_ordered_breaks_cycle(self):
        data = [{"_name": "A", "Parent": "B"}, {"_name": "B", "Parent": "A"}]
        out = StockGroupImporter._ordered(data)
        self.assertEqual({g["_name"] for g in out}, {"A", "B"})

    def test_account_topo_groups_breaks_cycle(self):
        data = [_node("A", "B"), _node("B", "A")]
        out = AccountImporter._topo_groups(data)
        self.assertEqual({g.name for g in out}, {"A", "B"})

    def test_cost_centre_ordered_breaks_cycle(self):
        data = [_node("A", "B"), _node("B", "A")]
        out = CostCentreImporter._ordered(data)
        self.assertEqual({c.name for c in out}, {"A", "B"})

    def test_self_parent_is_safe(self):
        self.assertEqual(
            {g.name for g in AccountImporter._topo_groups([_node("X", "X")])}, {"X"})

    def test_longer_cycle_breaks(self):
        data = [_node("A", "B"), _node("B", "C"), _node("C", "A")]
        out = CostCentreImporter._ordered(data)
        self.assertEqual({c.name for c in out}, {"A", "B", "C"})


class TestOpeningBalanceGuards(unittest.TestCase):
    """C2: a missing account/party drops only its line (with a warning); the
    opening entry still posts the rest instead of failing wholesale."""

    def _imp(self):
        return OpeningBalanceImporter(company="_T Co", abbr="TC")

    def test_account_line_skipped_when_account_missing(self):
        imp = self._imp()
        result = ImportResult("Journal Entry")
        accounts = [_node("Cash", None)]
        accounts[0].opening_balance = 5000.0
        accounts[0].is_group = 0
        accounts[0].opening_dr_cr = "Dr"
        with mock.patch("frappe.db") as db:
            db.exists.return_value = False
            lines = imp._account_lines(accounts, result)
        self.assertEqual(lines, [])
        self.assertEqual(result.warned, 1)
        self.assertEqual(result.failed, 0)

    def test_account_line_kept_when_account_exists(self):
        imp = self._imp()
        result = ImportResult("Journal Entry")
        acc = _node("Cash", None)
        acc.opening_balance, acc.is_group, acc.opening_dr_cr = 5000.0, 0, "Dr"
        with mock.patch("frappe.db") as db:
            db.exists.return_value = True
            lines = imp._account_lines([acc], result)
        self.assertEqual(len(lines), 1)
        self.assertEqual(result.warned, 0)

    def test_party_line_skipped_when_party_missing(self):
        imp = self._imp()
        result = ImportResult("Journal Entry")
        parties = [{"_name": "Acme", "OpeningBalance": "1000 Dr"}]
        with mock.patch("frappe.get_cached_value", return_value="Debtors - TC"), \
                mock.patch("frappe.db") as db:
            db.get_value.return_value = None  # not created
            lines = imp._party_lines(parties, "Customer", "default_receivable_account", result)
        self.assertEqual(lines, [])
        self.assertEqual(result.warned, 1)
        self.assertEqual(result.failed, 0)

    def test_party_line_uses_resolved_docname(self):
        # H1: the JE party must be the actual document name (e.g. a naming series),
        # not the Tally display name.
        imp = self._imp()
        result = ImportResult("Journal Entry")
        parties = [{"_name": "Acme", "OpeningBalance": "1000 Dr"}]
        with mock.patch("frappe.get_cached_value", return_value="Debtors - TC"), \
                mock.patch("frappe.db") as db:
            db.get_value.return_value = "CUST-0001"  # resolved docname differs from name
            lines = imp._party_lines(parties, "Customer", "default_receivable_account", result)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["party"], "CUST-0001")
        self.assertEqual(result.warned, 0)


if __name__ == "__main__":
    unittest.main()
