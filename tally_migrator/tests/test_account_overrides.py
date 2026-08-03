"""Tests for user-corrected account classification (the Preview-step root_type edit).

Two layers:
  * ``apply_account_overrides`` - the pure patch on the extracted COA (offline).
  * the consistency contract - an overridden account must re-home under a parent that
    matches its NEW root_type, so ERPNext never rejects a child/parent root mismatch.
    Verified at the ``AccountImporter._resolve_parent`` seam (the code that places each
    account), with the DB-touching root-group lookup stubbed.
"""
import types
import unittest
from unittest import mock

from tally_migrator.tally.extractors import AccountNode, ExtractedCOA
from tally_migrator.migration.overrides import apply_account_overrides
from tally_migrator.erpnext.importers import AccountImporter


def _acct(name, root, parent="Custom Group", account_type="", is_group=False,
          amount=1000.0, dr_cr="Dr"):
    return AccountNode(name=name, parent=parent, is_group=is_group, root_type=root,
                       account_type=account_type, is_reserved=False,
                       opening_balance=amount, opening_dr_cr=dr_cr)


def _coa(accounts):
    return ExtractedCOA(accounts=accounts, cost_centres=[])


class TestApplyAccountOverrides(unittest.TestCase):
    def test_reclassify_updates_root_account_type_and_rehomes(self):
        coa = _coa([_acct("Weird Ledger", "Liability", parent="Suspense",
                          account_type="")])
        log = []
        apply_account_overrides(
            coa, {"Account": {"Weird Ledger": {"root_type": "Income"}}}, log)
        node = coa.accounts[0]
        self.assertEqual(node.root_type, "Income")
        # account_type reset to the derived default for the new root...
        self.assertEqual(node.account_type, "Income Account")
        # ...and the parent cleared so the importer re-homes it under the new root.
        self.assertEqual(node.parent, "")
        self.assertEqual(log, [{
            "entity_type": "Account", "record_name": "Weird Ledger",
            "field": "root_type", "old": "Liability", "new": "Income"}])

    def test_move_to_balance_sheet_root_clears_pl_account_type(self):
        coa = _coa([_acct("Misclassified", "Income", account_type="Income Account")])
        apply_account_overrides(
            coa, {"Account": {"Misclassified": {"root_type": "Asset"}}})
        self.assertEqual(coa.accounts[0].account_type, "")

    def test_group_account_is_never_touched(self):
        coa = _coa([_acct("Some Group", "Asset", is_group=True)])
        apply_account_overrides(
            coa, {"Account": {"Some Group": {"root_type": "Income"}}})
        self.assertEqual(coa.accounts[0].root_type, "Asset")

    def test_no_op_and_blank_and_unknown_are_ignored(self):
        coa = _coa([
            _acct("A", "Asset"), _acct("B", "Asset"), _acct("C", "Asset")])
        log = []
        apply_account_overrides(coa, {"Account": {
            "A": {"root_type": "Asset"},     # same value - no effective change
            "B": {"root_type": ""},          # blank - ignored
            "C": {"root_type": "Bogus"},     # not a real root - ignored
        }}, log)
        self.assertEqual([a.root_type for a in coa.accounts], ["Asset"] * 3)
        # Untouched accounts keep their real parent (no accidental re-home).
        self.assertEqual([a.parent for a in coa.accounts], ["Custom Group"] * 3)
        self.assertEqual(log, [])

    def test_other_buckets_and_empty_overrides_ignored(self):
        coa = _coa([_acct("A", "Asset")])
        # Record-override buckets for parties/items must not affect the COA.
        apply_account_overrides(coa, {"Customer": {"A": {"root_type": "Income"}}})
        apply_account_overrides(coa, {})
        apply_account_overrides(coa, None)
        self.assertEqual(coa.accounts[0].root_type, "Asset")

    def test_unknown_account_name_is_a_no_op(self):
        coa = _coa([_acct("Real", "Asset")])
        apply_account_overrides(
            coa, {"Account": {"Ghost": {"root_type": "Income"}}})
        self.assertEqual(coa.accounts[0].root_type, "Asset")


class TestReclassifiedAccountRehomesToNewRoot(unittest.TestCase):
    """The consistency contract: after an override the account must be placed under a
    group whose root matches the NEW root_type. ``_resolve_parent`` returns the new
    root's group for a blank parent, so the re-home apply_account_overrides performs is
    exactly what the importer needs to avoid a root mismatch."""

    def _importer(self, root_group_for):
        imp = AccountImporter.__new__(AccountImporter)
        imp._redirect = {}
        imp.mode = "reuse"
        # Stub the DB-touching root-group lookup: return a sentinel group per root_type.
        imp._root_group = lambda root_type: root_group_for.get(root_type)
        return imp

    def test_blank_parent_after_override_routes_to_new_root_group(self):
        coa = _coa([_acct("Weird Ledger", "Liability", parent="Suspense")])
        apply_account_overrides(
            coa, {"Account": {"Weird Ledger": {"root_type": "Income"}}})
        node = coa.accounts[0]
        imp = self._importer({"Income": "Indirect Incomes - X",
                              "Liability": "Current Liabilities - X"})
        # It must resolve to the INCOME group (the new root), never the old liability one.
        self.assertEqual(imp._resolve_parent(node), "Indirect Incomes - X")


if __name__ == "__main__":
    unittest.main()
