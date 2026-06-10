"""
Regression tests for the Phase-1 critical fixes. Run via ``bench run-tests``
(they import ``tally_migrator.erpnext.importers``, which imports ``frappe``).

Covered:
- C2: opening balances skip - not fail - when a referenced account/party is missing.
- C3: hierarchy sorters break circular parents instead of recursing forever.
- H3: the per-company opening lock serialises opening posting, and the active-run
  guard refuses a concurrent run for the same company (a stale log does not block).

C1 (override persistence on re-run) and C4 (company-scoped warehouse idempotency)
are exercised by the live-DB integration tests in ``test_importer`` / manual re-run,
since they depend on inserted documents.
"""
import types
import unittest
from unittest import mock

import frappe

from tally_migrator.erpnext import importers
from tally_migrator.erpnext.importers import (
    OpeningBalanceImporter,
    WarehouseImporter,
    StockGroupImporter,
    AccountImporter,
    CostCentreImporter,
    ItemImporter,
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


class TestPLOpeningCostCenter(unittest.TestCase):
    """A P&L (Income/Expense) opening line needs a cost center or ERPNext rejects the
    whole Opening Entry batch on submit. Attach the company default; skip with a
    warning when none is set. Balance-sheet lines must stay cost-center-free."""

    def _imp(self):
        return OpeningBalanceImporter(company="_T Co", abbr="TC")

    def _ledger(self, root_type):
        n = _node("Sales", None)
        n.opening_balance, n.is_group, n.opening_dr_cr = 1000.0, 0, "Cr"
        n.root_type = root_type
        return n

    def test_pl_line_gets_default_cost_center(self):
        imp, result = self._imp(), ImportResult("Journal Entry")
        with mock.patch("frappe.db") as db, \
                mock.patch("frappe.get_cached_value", return_value="Main - TC"):
            db.exists.return_value = True
            lines = imp._account_lines([self._ledger("Income")], result)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["cost_center"], "Main - TC")

    def test_pl_line_skipped_when_no_default_cost_center(self):
        imp, result = self._imp(), ImportResult("Journal Entry")
        with mock.patch("frappe.db") as db, \
                mock.patch("frappe.get_cached_value", return_value=None):
            db.exists.return_value = True
            lines = imp._account_lines([self._ledger("Expense")], result)
        self.assertEqual(lines, [])
        self.assertEqual(result.warned, 1)
        self.assertEqual(result.failed, 0)

    def test_balance_sheet_line_has_no_cost_center(self):
        imp, result = self._imp(), ImportResult("Journal Entry")
        with mock.patch("frappe.db") as db, \
                mock.patch("frappe.get_cached_value", return_value="Main - TC"):
            db.exists.return_value = True
            lines = imp._account_lines([self._ledger("Asset")], result)
        self.assertEqual(len(lines), 1)
        self.assertNotIn("cost_center", lines[0])


class TestOpeningConcurrencyLock(unittest.TestCase):
    """H3: a self-expiring per-company Redis lock serialises opening posting so two
    concurrent runs can't both pass the existence check and double-post the books."""

    def test_lock_acquired_yields_true_and_releases(self):
        cache = mock.Mock()
        cache.set.return_value = True            # SETNX succeeded
        with mock.patch("frappe.cache", return_value=cache), \
                mock.patch("frappe.utils.now", return_value="2026-01-01 00:00:00"):
            with importers._company_opening_lock("Acme") as got:
                self.assertTrue(got)
            cache.set.assert_called_once()
            _, kwargs = cache.set.call_args
            self.assertTrue(kwargs.get("nx"))    # set only if absent
            self.assertTrue(kwargs.get("ex"))    # with a TTL, so a dead worker frees it
            cache.delete.assert_called_once()    # released on exit

    def test_lock_contended_yields_false_and_does_not_release(self):
        cache = mock.Mock()
        cache.set.return_value = None            # held by another run
        with mock.patch("frappe.cache", return_value=cache), \
                mock.patch("frappe.utils.now", return_value="2026-01-01 00:00:00"):
            with importers._company_opening_lock("Acme") as got:
                self.assertFalse(got)
            cache.delete.assert_not_called()     # never delete a lock we don't hold

    def test_cache_down_falls_back_to_acquired(self):
        cache = mock.Mock()
        cache.set.side_effect = RuntimeError("redis unavailable")
        with mock.patch("frappe.cache", return_value=cache), \
                mock.patch("frappe.utils.now", return_value="2026-01-01 00:00:00"):
            with importers._company_opening_lock("Acme") as got:
                self.assertTrue(got)             # degrade open; existence check backstops
            cache.delete.assert_not_called()

    def test_losing_run_stands_down_without_posting(self):
        # When the lock is held by another run, the facade returns a skipped result
        # with a warning instead of building/posting an opening entry.
        from tally_migrator.erpnext.importers import ERPNextImporter
        with mock.patch("frappe.get_value", return_value="TC"):
            imp = ERPNextImporter("_T Co")
        with mock.patch.object(importers, "_company_opening_lock") as lock:
            lock.return_value.__enter__ = mock.Mock(return_value=False)
            lock.return_value.__exit__ = mock.Mock(return_value=False)
            result = imp.import_opening_balances([], [], [], "2026-01-01")
        self.assertEqual(result.created, 0)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.warned, 1)
        self.assertEqual(result.failed, 0)


class TestActiveRunGuard(unittest.TestCase):
    """H3: a second run is refused while a recent 'Running' log exists for the
    same company; a stale 'Running' log (crashed worker) must NOT block re-runs."""

    def _api(self):
        from tally_migrator import api
        return api

    def test_no_running_log_allows(self):
        with mock.patch("frappe.get_all", return_value=[]):
            self._api()._assert_no_active_run("Acme")   # must not raise

    def test_blank_company_allows(self):
        # No company yet (nothing to guard); must not query or raise.
        self._api()._assert_no_active_run("")

    def test_recent_running_log_blocks(self):
        rows = [frappe._dict({"name": "LOG-1", "modified": "2026-01-01 00:00:00"})]
        with mock.patch("frappe.get_all", return_value=rows), \
                mock.patch("frappe.utils.now", return_value="2026-01-01 00:00:30"), \
                mock.patch("frappe.utils.time_diff_in_seconds", return_value=30), \
                mock.patch("frappe.utils.pretty_date", return_value="just now"):
            with self.assertRaises(frappe.ValidationError):
                self._api()._assert_no_active_run("Acme")

    def test_stale_running_log_allows(self):
        rows = [frappe._dict({"name": "LOG-1", "modified": "2026-01-01 00:00:00"})]
        with mock.patch("frappe.get_all", return_value=rows), \
                mock.patch("frappe.utils.now", return_value="ignored"), \
                mock.patch("frappe.utils.time_diff_in_seconds", return_value=10 * 3600):
            self._api()._assert_no_active_run("Acme")   # stale -> must not raise


class TestItemHsnRecovery(unittest.TestCase):
    """An India-Compliance HSN rejection must not lose the item: the importer
    retries once with the HSN cleared so the item still lands (HSN filled later)."""

    def _imp(self):
        return ItemImporter(company="_T Co", abbr="TC")

    def test_recover_clears_invalid_hsn_and_warns(self):
        data = {"item_code": "X", "item_name": "X", "gst_hsn_code": "9999"}
        out = self._imp().recover_insert(
            data, Exception("Could not find Row #1: GST HSN Code: 9999"))
        self.assertIsNotNone(out)
        retry, warning = out
        self.assertEqual(retry["gst_hsn_code"], "")     # cleared for the retry
        self.assertIn("HSN", warning)
        self.assertEqual(data["gst_hsn_code"], "9999")  # original dict not mutated

    def test_no_recovery_when_hsn_blank(self):
        # A blank HSN can't be salvaged by clearing it - nothing to clear.
        out = self._imp().recover_insert(
            {"item_code": "X", "gst_hsn_code": ""}, Exception("HSN is mandatory"))
        self.assertIsNone(out)

    def test_no_recovery_for_unrelated_error(self):
        out = self._imp().recover_insert(
            {"item_code": "X", "gst_hsn_code": "9999"}, Exception("some other failure"))
        self.assertIsNone(out)


class TestHsnValidationToggle(unittest.TestCase):
    """The item import temporarily disables India Compliance's HSN validation and
    restores it, self-healing via a cache marker if a worker is hard-killed."""

    def test_noop_when_india_compliance_absent(self):
        with mock.patch.object(importers, "_hsn_validation_field_present",
                               return_value=False):
            with importers._hsn_validation_suspended() as suspended:
                self.assertFalse(suspended)

    def test_disables_on_entry_and_restores_on_exit(self):
        calls = []
        with mock.patch.object(importers, "_hsn_validation_field_present",
                               return_value=True), \
                mock.patch("frappe.db.get_single_value", return_value=1), \
                mock.patch.object(importers, "_set_hsn_validation",
                                  side_effect=lambda v: calls.append(v)), \
                mock.patch("frappe.cache") as cache:
            with importers._hsn_validation_suspended() as suspended:
                self.assertTrue(suspended)
                self.assertEqual(calls, [0])     # disabled while inside
            self.assertEqual(calls, [0, 1])      # re-enabled on exit
            cache.return_value.set_value.assert_called_once()
            cache.return_value.delete_value.assert_called_once()

    def test_noop_when_setting_already_off(self):
        calls = []
        with mock.patch.object(importers, "_hsn_validation_field_present",
                               return_value=True), \
                mock.patch("frappe.db.get_single_value", return_value=0), \
                mock.patch.object(importers, "_set_hsn_validation",
                                  side_effect=lambda v: calls.append(v)):
            with importers._hsn_validation_suspended() as suspended:
                self.assertFalse(suspended)
            self.assertEqual(calls, [])          # user's choice left untouched

    def test_restore_guard_reenables_when_marker_present(self):
        with mock.patch("frappe.cache") as cache, \
                mock.patch.object(importers, "_hsn_validation_field_present",
                                  return_value=True), \
                mock.patch.object(importers, "_set_hsn_validation") as setter:
            cache.return_value.get_value.return_value = "1"
            importers._restore_hsn_validation()
            setter.assert_called_once_with(1)    # fail-safe: validation back ON
            cache.return_value.delete_value.assert_called_once()

    def test_restore_guard_noop_without_marker(self):
        with mock.patch("frappe.cache") as cache, \
                mock.patch.object(importers, "_set_hsn_validation") as setter:
            cache.return_value.get_value.return_value = None
            importers._restore_hsn_validation()
            setter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
