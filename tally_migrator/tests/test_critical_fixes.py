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
    PartyOpeningImporter,
    WarehouseImporter,
    StockGroupImporter,
    AccountImporter,
    CostCentreImporter,
    ItemImporter,
    ImportResult,
)
from tally_migrator.tally.extractors import BillAllocation


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
        accounts[0].root_type = "Asset"
        with mock.patch("frappe.db.exists", return_value=False):
            lines = imp._account_lines(accounts, result)
        self.assertEqual(lines, [])
        self.assertEqual(result.warned, 1)
        self.assertEqual(result.failed, 0)

    def test_account_line_kept_when_account_exists(self):
        imp = self._imp()
        result = ImportResult("Journal Entry")
        acc = _node("Cash", None)
        acc.opening_balance, acc.is_group, acc.opening_dr_cr = 5000.0, 0, "Dr"
        acc.root_type = "Asset"
        with mock.patch("frappe.db.exists", return_value=True):
            lines = imp._account_lines([acc], result)
        self.assertEqual(len(lines), 1)
        self.assertEqual(result.warned, 0)

    def test_party_line_skipped_when_party_missing(self):
        imp = self._imp()
        result = ImportResult("Journal Entry")
        parties = [{"_name": "Acme", "OpeningBalance": "1000 Dr"}]
        with mock.patch("frappe.get_cached_value", return_value="Debtors - TC"), \
                mock.patch("frappe.db.get_value", return_value=None):  # not created
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
                mock.patch("frappe.db.get_value", return_value="CUST-0001"):  # resolved docname differs from name
            lines = imp._party_lines(parties, "Customer", "default_receivable_account", result)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["party"], "CUST-0001")
        self.assertEqual(result.warned, 0)


class TestPLOpeningSkipped(unittest.TestCase):
    """ERPNext forbids an Income/Expense (P&L) account from carrying an opening balance
    (GL Entry.check_pl_account throws in an Opening Entry), so the importer must skip
    those lines with a warning - never post them - while balance-sheet lines post
    normally. The skipped amount stays in the Temporary Opening difference."""

    def _imp(self):
        return OpeningBalanceImporter(company="_T Co", abbr="TC")

    def _ledger(self, root_type):
        n = _node("Sales", None)
        n.opening_balance, n.is_group, n.opening_dr_cr = 1000.0, 0, "Cr"
        n.root_type = root_type
        return n

    def test_income_line_skipped_with_warning(self):
        imp, result = self._imp(), ImportResult("Journal Entry")
        with mock.patch("frappe.db") as db:
            db.exists.return_value = True
            lines = imp._account_lines([self._ledger("Income")], result)
        self.assertEqual(lines, [])
        self.assertEqual(result.warned, 1)
        self.assertEqual(result.failed, 0)

    def test_expense_line_skipped_with_warning(self):
        imp, result = self._imp(), ImportResult("Journal Entry")
        with mock.patch("frappe.db") as db:
            db.exists.return_value = True
            lines = imp._account_lines([self._ledger("Expense")], result)
        self.assertEqual(lines, [])
        self.assertEqual(result.warned, 1)

    def test_balance_sheet_line_posts_without_cost_center(self):
        imp, result = self._imp(), ImportResult("Journal Entry")
        with mock.patch("frappe.db") as db:
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
        with mock.patch.object(importers.orchestrator, "_company_opening_lock") as lock:
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

    def test_live_async_job_blocks_regardless_of_age(self):
        # An enqueued run with a still-alive RQ job blocks even past the age cap that
        # would have freed a sync run - liveness wins over age.
        rows = [frappe._dict(
            {"name": "LOG-1", "modified": "2026-01-01 00:00:00", "job_id": "tally-masters-LOG-1"})]
        with mock.patch("frappe.get_all", return_value=rows), \
                mock.patch.object(self._api(), "_is_job_alive", return_value=True), \
                mock.patch("frappe.utils.time_diff_in_seconds", return_value=10 * 3600), \
                mock.patch("frappe.utils.now", return_value="ignored"), \
                mock.patch("frappe.utils.pretty_date", return_value="earlier"):
            with self.assertRaises(frappe.ValidationError):
                self._api()._assert_no_active_run("Acme")

    def test_dead_async_job_allows_even_when_recent(self):
        # A crashed worker leaves a recent 'Running' log, but its RQ job is gone, so a
        # re-run is allowed immediately rather than waiting out the age cap.
        rows = [frappe._dict(
            {"name": "LOG-1", "modified": "2026-01-01 00:00:00", "job_id": "tally-masters-LOG-1"})]
        with mock.patch("frappe.get_all", return_value=rows), \
                mock.patch.object(self._api(), "_is_job_alive", return_value=False):
            self._api()._assert_no_active_run("Acme")   # dead job -> must not raise


class TestRunLiveness(unittest.TestCase):
    """The log form asks run_liveness whether a 'Running' log's worker is alive, so a
    hard-killed run shows a 'stopped' state instead of 'still running' forever."""

    def _api(self):
        from tally_migrator import api
        return api

    def test_terminal_status_is_not_alive(self):
        row = frappe._dict({"status": "Completed", "job_id": "j", "modified": "x"})
        with mock.patch("frappe.db.get_value", return_value=row):
            out = self._api().run_liveness("LOG-1")
        self.assertEqual(out, {"status": "Completed", "alive": False})

    def test_running_with_live_job_is_alive(self):
        row = frappe._dict({"status": "Running", "job_id": "tally-masters-LOG-1", "modified": "x"})
        with mock.patch("frappe.db.get_value", return_value=row), \
                mock.patch.object(self._api(), "_is_job_alive", return_value=True):
            out = self._api().run_liveness("LOG-1")
        self.assertEqual(out, {"status": "Running", "alive": True})

    def test_running_with_dead_job_is_not_alive(self):
        row = frappe._dict({"status": "Running", "job_id": "tally-masters-LOG-1", "modified": "x"})
        with mock.patch("frappe.db.get_value", return_value=row), \
                mock.patch.object(self._api(), "_is_job_alive", return_value=False):
            out = self._api().run_liveness("LOG-1")
        self.assertEqual(out, {"status": "Running", "alive": False})

    def test_jobless_running_uses_age_cap(self):
        # A legacy/sync run with no job id falls back to the staleness age cap.
        row = frappe._dict({"status": "Running", "job_id": None, "modified": "2026-01-01 00:00:00"})
        with mock.patch("frappe.db.get_value", return_value=row), \
                mock.patch("frappe.utils.now", return_value="ignored"), \
                mock.patch("frappe.utils.time_diff_in_seconds", return_value=10 * 3600):
            out = self._api().run_liveness("LOG-1")
        self.assertEqual(out["alive"], False)

    def test_unknown_log_returns_empty(self):
        with mock.patch("frappe.db.get_value", return_value=None):
            self.assertEqual(self._api().run_liveness("NOPE"), {})


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
        with mock.patch.object(importers.hsn, "_hsn_validation_field_present",
                               return_value=False):
            with importers._hsn_validation_suspended() as suspended:
                self.assertFalse(suspended)

    def test_disables_on_entry_and_restores_on_exit(self):
        calls = []
        with mock.patch.object(importers.hsn, "_hsn_validation_field_present",
                               return_value=True), \
                mock.patch("frappe.db.get_single_value", return_value=1), \
                mock.patch.object(importers.hsn, "_set_hsn_validation",
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
        with mock.patch.object(importers.hsn, "_hsn_validation_field_present",
                               return_value=True), \
                mock.patch("frappe.db.get_single_value", return_value=0), \
                mock.patch.object(importers.hsn, "_set_hsn_validation",
                                  side_effect=lambda v: calls.append(v)):
            with importers._hsn_validation_suspended() as suspended:
                self.assertFalse(suspended)
            self.assertEqual(calls, [])          # user's choice left untouched

    def test_restore_guard_reenables_when_marker_present(self):
        with mock.patch("frappe.cache") as cache, \
                mock.patch.object(importers.hsn, "_hsn_validation_field_present",
                                  return_value=True), \
                mock.patch.object(importers.hsn, "_set_hsn_validation") as setter:
            cache.return_value.get_value.return_value = "1"
            importers._restore_hsn_validation()
            setter.assert_called_once_with(1)    # fail-safe: validation back ON
            cache.return_value.delete_value.assert_called_once()

    def test_restore_guard_noop_without_marker(self):
        with mock.patch("frappe.cache") as cache, \
                mock.patch.object(importers.hsn, "_set_hsn_validation") as setter:
            cache.return_value.get_value.return_value = None
            importers._restore_hsn_validation()
            setter.assert_not_called()


class TestPartyOpeningClassification(unittest.TestCase):
    """Per-bill routing: outstanding invoice (natural side) vs advance (opposite
    side, or Tally's ISADVANCE flag)."""

    def _c(self, party_type, signed, is_advance=False):
        return PartyOpeningImporter._classify(party_type, signed, is_advance)

    def test_customer_dr_is_invoice(self):
        self.assertEqual(self._c("Customer", 5000), "invoice")     # receivable

    def test_customer_cr_is_advance(self):
        self.assertEqual(self._c("Customer", -5000), "advance")    # credit balance

    def test_supplier_cr_is_invoice(self):
        self.assertEqual(self._c("Supplier", -5000), "invoice")    # payable

    def test_supplier_dr_is_advance(self):
        self.assertEqual(self._c("Supplier", 5000), "advance")     # advance paid

    def test_advance_flag_forces_advance_on_natural_side(self):
        # A customer Dr bill is normally an invoice, but Tally's ISADVANCE wins.
        self.assertEqual(self._c("Customer", 5000, is_advance=True), "advance")

    def test_signed_dr_positive(self):
        self.assertEqual(PartyOpeningImporter._signed(100.0, "Dr"), 100.0)
        self.assertEqual(PartyOpeningImporter._signed(100.0, "Cr"), -100.0)


class TestPartyOpeningOrchestration(unittest.TestCase):
    """_process routes bills, reconciles to the ledger opening, and plugs the gap.
    _emit is stubbed to capture routing decisions without touching ERPNext."""

    def _imp(self):
        return PartyOpeningImporter("_T Co", "TC", "2026-04-01")

    def _run(self, parties, party_type, by_party):
        imp = self._imp()
        result = ImportResult("Opening Invoice")
        captured = []

        def fake_emit(pt, party, tally_name, signed, bill_no, bill_date,
                      is_advance, seen, result, real_bill=False):
            captured.append({"signed": round(signed, 2), "bill_no": bill_no,
                             "is_advance": is_advance, "real_bill": real_bill})

        with mock.patch.object(imp, "_emit", side_effect=fake_emit), \
                mock.patch.object(imp, "_is_foreign_currency_party", return_value=False), \
                mock.patch("frappe.db.get_value", return_value="RESOLVED-NAME"):
            imp._process(parties, party_type, by_party, set(), result)
        return captured, result

    def test_bills_that_tie_emit_each_with_no_plug(self):
        # ABC: ledger Dr 30000, three Dr bills summing 30000 -> 3 emits, no plug.
        bills = [BillAllocation("ABC", "ABC/1", "2020-03-10", 3000.0, "Dr", False),
                 BillAllocation("ABC", "ABC/2", "2020-03-12", 6000.0, "Dr", False),
                 BillAllocation("ABC", "ABC/3", "2020-03-14", 21000.0, "Dr", False)]
        captured, result = self._run(
            [{"_name": "ABC", "OpeningBalance": "-30000"}], "Customer",
            {"ABC": bills})
        self.assertEqual(len(captured), 3)
        self.assertEqual({c["bill_no"] for c in captured}, {"ABC/1", "ABC/2", "ABC/3"})
        self.assertTrue(all(c["real_bill"] for c in captured))   # named after Tally id
        self.assertEqual(result.warned, 0)

    def test_no_bill_party_emits_single_lump_no_warning(self):
        captured, result = self._run(
            [{"_name": "Solo", "OpeningBalance": "-8000"}], "Customer", {})
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["bill_no"], "Opening")     # lump label
        self.assertFalse(captured[0]["real_bill"])              # not a Tally bill id
        self.assertEqual(captured[0]["signed"], 8000.0)         # Dr receivable
        self.assertEqual(result.warned, 0)                      # normal, not a gap

    def test_bill_ledger_mismatch_plugs_on_account_with_warning(self):
        # National Traders: ledger Dr 15400, single Cr 15000 bill -> bill emit +
        # an On Account plug for the residual, plus a warning.
        bills = [BillAllocation("NT", "6542", "2020-03-20", 15000.0, "Cr", False)]
        captured, result = self._run(
            [{"_name": "NT", "OpeningBalance": "-15400"}], "Customer", {"NT": bills})
        self.assertEqual(len(captured), 2)
        plug = [c for c in captured if c["bill_no"] == "On Account"]
        self.assertEqual(len(plug), 1)
        self.assertEqual(plug[0]["signed"], 30400.0)            # 15400 - (-15000)
        self.assertEqual(result.warned, 1)

    def test_missing_party_skipped_with_warning(self):
        imp = self._imp()
        result = ImportResult("Opening Invoice")
        with mock.patch.object(imp, "_emit") as emit, \
                mock.patch.object(imp, "_is_foreign_currency_party", return_value=False), \
                mock.patch("frappe.db.get_value", return_value=None):  # never created
            imp._process([{"_name": "Ghost", "OpeningBalance": "-100"}],
                         "Customer", {}, set(), result)
        emit.assert_not_called()
        self.assertEqual(result.warned, 1)

    def test_foreign_currency_party_skipped_with_warning(self):
        # A party billing in a non-company currency is skipped (the export carries
        # no rate); nothing is emitted and the user is warned to enter it manually.
        imp = self._imp()
        result = ImportResult("Opening Invoice")
        with mock.patch.object(imp, "_emit") as emit, \
                mock.patch.object(imp, "_is_foreign_currency_party", return_value=True), \
                mock.patch("frappe.db.get_value", return_value="RESOLVED-NAME"):
            imp._process([{"_name": "USD Corp", "OpeningBalance": "-5000"}],
                         "Customer", {}, set(), result)
        emit.assert_not_called()
        self.assertEqual(result.warned, 1)

    def test_rerun_skips_already_posted_bills(self):
        # Idempotency at the orchestration level: a second run whose markers are
        # all already in `seen` emits nothing new (every _emit is a no-op skip).
        bills = [BillAllocation("ABC", "ABC/1", "2020-03-10", 30000.0, "Dr", False)]
        imp = self._imp()
        result = ImportResult("Opening Invoice")
        seen = {"Tally opening: ABC | ABC/1"}
        with mock.patch.object(imp, "_is_foreign_currency_party", return_value=False), \
                mock.patch("frappe.db.get_value", return_value="RESOLVED-NAME"), \
                mock.patch("frappe.get_doc") as get_doc:
            imp._process([{"_name": "ABC", "OpeningBalance": "-30000"}],
                         "Customer", {"ABC": bills}, seen, result)
        get_doc.assert_not_called()       # nothing re-created
        self.assertEqual(result.created, 0)
        self.assertEqual(result.skipped, 1)


class TestPartyOpeningEmit(unittest.TestCase):
    """_emit honours the idempotency marker and records create/error outcomes."""

    def _imp(self):
        return PartyOpeningImporter("_T Co", "TC", "2026-04-01")

    def test_existing_marker_is_skipped(self):
        imp, result = self._imp(), ImportResult("Opening Invoice")
        seen = {"Tally opening: ABC | ABC/1"}
        with mock.patch("frappe.get_doc") as get_doc:
            imp._emit("Customer", "CUST", "ABC", 3000.0, "ABC/1", "2020-03-10",
                      False, seen, result)
        get_doc.assert_not_called()
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.created, 0)

    def test_create_adds_marker_and_counts(self):
        imp, result = self._imp(), ImportResult("Opening Invoice")
        seen = set()
        doc = mock.Mock(name="SI-OPEN-1")
        doc.name = "SI-OPEN-1"
        with mock.patch.object(imp, "_invoice_dict", return_value={"doctype": "Sales Invoice"}), \
                mock.patch("frappe.get_doc", return_value=doc), \
                mock.patch("frappe.db.commit"):
            imp._emit("Customer", "CUST", "ABC", 3000.0, "ABC/1", "2020-03-10",
                      False, seen, result)
        doc.insert.assert_called_once()
        doc.submit.assert_called_once()
        self.assertEqual(result.created, 1)
        self.assertIn("Tally opening: ABC | ABC/1", seen)

    def test_failure_rolls_back_and_records_error(self):
        imp, result = self._imp(), ImportResult("Opening Invoice")
        with mock.patch.object(imp, "_invoice_dict", return_value={}), \
                mock.patch("frappe.get_doc", side_effect=Exception("boom")), \
                mock.patch("frappe.db.rollback") as rollback:
            imp._emit("Customer", "CUST", "ABC", 3000.0, "ABC/1", "2020-03-10",
                      False, set(), result)
            rollback.assert_called_once()
        self.assertEqual(result.failed, 1)
        self.assertEqual(result.created, 0)

    def test_below_threshold_is_noop(self):
        imp, result = self._imp(), ImportResult("Opening Invoice")
        with mock.patch("frappe.get_doc") as get_doc:
            imp._emit("Customer", "CUST", "ABC", 0.4, "ABC/1", "", False,
                      set(), result)
        get_doc.assert_not_called()
        self.assertEqual(result.created, 0)

    def test_real_bill_names_invoice_after_tally_id(self):
        # The opening invoice is named after the Tally bill id (set_name), so the
        # ERPNext document id reconciles directly against the Tally invoice.
        imp, result = self._imp(), ImportResult("Opening Invoice")
        doc = mock.Mock()
        doc.name = "ABC/1"
        with mock.patch.object(imp, "_invoice_dict", return_value={"doctype": "Sales Invoice"}), \
                mock.patch("frappe.get_doc", return_value=doc), \
                mock.patch("frappe.db.commit"):
            imp._emit("Customer", "CUST", "ABC", 3000.0, "ABC/1", "2020-03-10",
                      False, set(), result, real_bill=True)
        doc.insert.assert_called_once_with(ignore_permissions=True, set_name="ABC/1")
        self.assertEqual(result.created, 1)

    def test_lump_opening_is_not_named_after_a_bill(self):
        # The lump 'Opening' plug is not a real bill id, so it auto-names.
        imp, result = self._imp(), ImportResult("Opening Invoice")
        doc = mock.Mock()
        doc.name = "ACC-SINV-0001"
        with mock.patch.object(imp, "_invoice_dict", return_value={"doctype": "Sales Invoice"}), \
                mock.patch("frappe.get_doc", return_value=doc), \
                mock.patch("frappe.db.commit"):
            imp._emit("Customer", "CUST", "Solo", 8000.0, "Opening", "2026-04-01",
                      False, set(), result, real_bill=False)
        doc.insert.assert_called_once_with(ignore_permissions=True)

    def test_duplicate_bill_id_falls_back_to_autoname_with_warning(self):
        # Two parties sharing a bill id: the second can't reuse it as the document id,
        # so it auto-names and warns rather than losing the opening.
        imp, result = self._imp(), ImportResult("Opening Invoice")
        clash = mock.Mock()
        clash.insert.side_effect = frappe.DuplicateEntryError("dup")
        fallback = mock.Mock()
        fallback.name = "ACC-SINV-0002"
        with mock.patch.object(imp, "_invoice_dict", return_value={"doctype": "Sales Invoice"}), \
                mock.patch("frappe.get_doc", side_effect=[clash, fallback]), \
                mock.patch("frappe.db.commit"), mock.patch("frappe.db.rollback"):
            imp._emit("Customer", "CUST", "ABC", 3000.0, "Bill 1", "2020-03-10",
                      False, set(), result, real_bill=True)
        fallback.insert.assert_called_once_with(ignore_permissions=True)
        self.assertEqual(result.created, 1)
        self.assertEqual(result.warned, 1)


class TestPartyOpeningDocBuilders(unittest.TestCase):
    """The Sales/Purchase Invoice and Payment Entry dicts have the right shape."""

    def _imp(self):
        imp = PartyOpeningImporter("_T Co", "TC", "2026-04-01")
        imp._temp, imp._cc, imp._uom, imp._cur = (
            "Temporary Opening - TC", "Main - TC", "Nos", "INR")
        return imp

    def test_sales_invoice_dict(self):
        d = self._imp()._invoice_dict(
            "Customer", "CUST", 3000.0, "ABC/1", "2020-03-10", "marker")
        self.assertEqual(d["doctype"], "Sales Invoice")
        self.assertEqual(d["is_opening"], "Yes")
        self.assertEqual(d["customer"], "CUST")
        self.assertEqual(d["posting_date"], "2026-04-01")   # opening date, not bill
        self.assertEqual(d["items"][0]["rate"], 3000.0)
        self.assertEqual(d["items"][0]["income_account"], "Temporary Opening - TC")
        self.assertEqual(d["items"][0]["cost_center"], "Main - TC")
        self.assertNotIn("bill_no", d)                      # Sales Invoice has none

    def test_purchase_invoice_dict_preserves_bill_ref(self):
        d = self._imp()._invoice_dict(
            "Supplier", "SUPP", 7000.0, "Bill 1", "2020-03-10", "marker")
        self.assertEqual(d["doctype"], "Purchase Invoice")
        self.assertEqual(d["supplier"], "SUPP")
        self.assertEqual(d["items"][0]["expense_account"], "Temporary Opening - TC")
        self.assertEqual(d["bill_no"], "Bill 1")            # native supplier-bill no
        self.assertEqual(d["bill_date"], "2020-03-10")

    def test_customer_advance_payment_entry(self):
        imp = self._imp()
        with mock.patch("frappe.get_cached_value", return_value="Debtors - TC"):
            d = imp._advance_dict("Customer", "CUST", 2000.0, "ADV-1", "marker")
        self.assertEqual(d["doctype"], "Payment Entry")
        self.assertEqual(d["payment_type"], "Receive")
        self.assertEqual(d["paid_from"], "Debtors - TC")     # from receivable
        self.assertEqual(d["paid_to"], "Temporary Opening - TC")
        self.assertEqual(d["paid_amount"], 2000.0)

    def test_supplier_advance_payment_entry(self):
        imp = self._imp()
        with mock.patch("frappe.get_cached_value", return_value="Creditors - TC"):
            d = imp._advance_dict("Supplier", "SUPP", 2000.0, "ADV-2", "marker")
        self.assertEqual(d["payment_type"], "Pay")
        self.assertEqual(d["paid_from"], "Temporary Opening - TC")
        self.assertEqual(d["paid_to"], "Creditors - TC")     # to payable


class TestPartyOpeningForeignCurrency(unittest.TestCase):
    """_is_foreign_currency_party: blank/company currency is local; anything else
    is foreign (and so skipped invoice-wise upstream)."""

    def _imp(self):
        imp = PartyOpeningImporter("_T Co", "TC", "2026-04-01")
        imp._cur = "INR"   # company currency resolved
        return imp

    def test_blank_party_currency_is_local(self):
        imp = self._imp()
        with mock.patch("frappe.db.get_value", return_value=None):
            self.assertFalse(imp._is_foreign_currency_party("Customer", "CUST"))

    def test_same_currency_is_local(self):
        imp = self._imp()
        with mock.patch("frappe.db.get_value", return_value="INR"):
            self.assertFalse(imp._is_foreign_currency_party("Customer", "CUST"))

    def test_other_currency_is_foreign(self):
        imp = self._imp()
        with mock.patch("frappe.db.get_value", return_value="USD"):
            self.assertTrue(imp._is_foreign_currency_party("Supplier", "SUPP"))


if __name__ == "__main__":
    unittest.main()
