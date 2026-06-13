"""Unit tests for the read-only reconciliation trial balance (pure parts, no DB)."""
import unittest
from types import SimpleNamespace
from unittest import mock

from tally_migrator.tally.extractors import AccountNode, ExtractedCOA, ExtractedMasters
from tally_migrator.migration.reconciliation import source_totals, compare


def _acct(name, root, amount, dr_cr, is_group=False):
    return AccountNode(name=name, parent="", is_group=is_group, root_type=root,
                       account_type="", is_reserved=False,
                       opening_balance=amount, opening_dr_cr=dr_cr)


def _coa(accounts):
    return ExtractedCOA(accounts=accounts, cost_centres=[])


def _masters(customers=None, suppliers=None, items=None):
    return ExtractedMasters(
        customers=customers or [], suppliers=suppliers or [],
        items=items or [], warehouses=[])


def _party(name, opening):
    return {"_name": name, "OpeningBalance": opening}


def _item(name, opening, rate="", value=""):
    return {"_name": name, "OpeningBalance": opening,
            "OpeningRate": rate, "OpeningValue": value, "StandardCost": ""}


def _classes(s):
    return {c["root"]: c["side"] for c in s["classes"]}


class TestSourceTotals(unittest.TestCase):
    def test_ledger_classes_by_root_type(self):
        # Tally sign convention: Dr = negative XML, Cr = positive XML. An asset lands
        # Dr, equity lands Cr - derived from root_type, no account-name rules.
        coa = _coa([
            _acct("HDFC Bank", "Asset", 10200, "Dr"),
            _acct("Cash", "Asset", 5000, "Dr"),
            _acct("Capital", "Equity", 100000, "Cr"),
        ])
        cls = _classes(source_totals(coa, _masters()))
        self.assertEqual(cls["Asset"], {"amount": 15200.0, "dr_cr": "Dr"})
        self.assertEqual(cls["LiabEquity"], {"amount": 100000.0, "dr_cr": "Cr"})

    def test_pl_openings_fold_into_temporary_opening(self):
        # ERPNext forbids posting a P&L (Income/Expense) opening, so it is never emitted
        # as its own class - the amount stays inside the Temporary Opening difference,
        # matching what ERPNext actually holds, so the trial balance still reconciles.
        coa = _coa([
            _acct("HDFC Bank", "Asset", 10200, "Dr"),
            _acct("Discount Received", "Income", 1900, "Cr"),
            _acct("Rent", "Expense", 2000, "Dr"),
        ])
        s = source_totals(coa, _masters())
        cls = _classes(s)
        self.assertNotIn("Income", cls)
        self.assertNotIn("Expense", cls)
        # Only the asset (10200 Dr) drives the contra; the P&L lines are folded in.
        self.assertEqual(s["temporary_opening"], {"amount": 10200.0, "dr_cr": "Cr"})

    def test_liability_and_equity_merge_into_one_class(self):
        # Tally calls capital Equity, ERPNext often roots it under Liability; merging
        # them avoids a false mismatch. A loan (Liability) + capital (Equity) net into
        # the single "Liabilities & Equity" row.
        coa = _coa([
            _acct("Bank Loan", "Liability", 40000, "Cr"),
            _acct("Capital", "Equity", 100000, "Cr"),
        ])
        rows = source_totals(coa, _masters())["classes"]
        labels = {r["label"]: r["side"] for r in rows}
        self.assertEqual(labels["Liabilities & Equity"], {"amount": 140000.0, "dr_cr": "Cr"})

    def test_zero_and_group_classes_omitted(self):
        coa = _coa([
            _acct("Current Assets", "Asset", 0, "", is_group=True),
            _acct("Idle Ledger", "Income", 0, ""),
            _acct("HDFC Bank", "Asset", 5000, "Dr"),
        ])
        cls = _classes(source_totals(coa, _masters()))
        self.assertIn("Asset", cls)
        self.assertNotIn("Income", cls)        # zero -> omitted

    def test_receivables_payables_from_party_openings(self):
        m = _masters(
            customers=[_party("Acme", "-30000")],
            suppliers=[_party("Globex", "20000")])
        s = source_totals(_coa([]), m)
        self.assertEqual(s["receivables"], {"amount": 30000.0, "dr_cr": "Dr"})
        self.assertEqual(s["payables"], {"amount": 20000.0, "dr_cr": "Cr"})

    def test_stock_value_and_count(self):
        m = _masters(items=[
            _item("Mouse", "100 Nos", rate="1.00/Nos"),     # 100 x 1 = 100
            _item("Pen", "10 Nos", value="-250.00"),         # |value| = 250
            _item("Free", "5 Nos"),                           # no value -> 0
        ])
        s = source_totals(_coa([]), m)
        self.assertEqual(s["stock"]["amount"], 350.0)
        self.assertEqual(s["stock_items"], 3)

    def test_temporary_opening_equals_tally_opening_difference(self):
        coa = _coa([
            _acct("HDFC Bank", "Asset", 10200, "Dr"),
            _acct("Capital", "Equity", 100000, "Cr"),
        ])
        s = source_totals(coa, _masters())
        self.assertEqual(s["temporary_opening"], {"amount": 89800.0, "dr_cr": "Dr"})

    def test_fully_balanced_books_leave_zero_temporary_opening(self):
        coa = _coa([
            _acct("HDFC Bank", "Asset", 100000, "Dr"),
            _acct("Capital", "Equity", 100000, "Cr"),
        ])
        s = source_totals(coa, _masters())
        self.assertEqual(s["temporary_opening"], {"amount": 0.0, "dr_cr": ""})


class TestCompare(unittest.TestCase):
    def _src(self):
        return source_totals(
            _coa([_acct("HDFC Bank", "Asset", 10200, "Dr"),
                  _acct("Capital", "Equity", 100000, "Cr")]),
            _masters(customers=[_party("Acme", "-30000")]))

    def _matching_erp(self, src):
        return {
            "available": True,
            "classes": {c["root"]: c["side"] for c in src["classes"]},
            "receivables": src["receivables"],
            "payables": src["payables"],
            "stock": src["stock"],
            "temporary_opening": src["temporary_opening"],
        }

    def test_source_trial_balance_is_balanced(self):
        out = compare(self._src(), {"available": False})
        self.assertTrue(out["total"]["source_balanced"])
        self.assertAlmostEqual(out["total"]["source"]["dr"], out["total"]["source"]["cr"])

    def test_source_only_when_erpnext_unavailable(self):
        out = compare(self._src(), {"available": False})
        self.assertEqual(out["verdict"], "source_only")
        self.assertTrue(all(not r["has_erpnext"] for r in out["rows"]))

    def test_reconciled_when_all_match(self):
        src = self._src()
        out = compare(src, self._matching_erp(src))
        self.assertEqual(out["verdict"], "reconciled")
        self.assertTrue(all(r["match"] for r in out["rows"]))
        self.assertTrue(out["total"]["erpnext_balanced"])

    def test_review_when_a_class_diverges(self):
        src = self._src()
        erp = self._matching_erp(src)
        erp["classes"]["Asset"] = {"amount": 999.0, "dr_cr": "Dr"}   # wrong
        out = compare(src, erp)
        self.assertEqual(out["verdict"], "review")
        asset = next(r for r in out["rows"] if r["key"] == "class_Asset")
        self.assertFalse(asset["match"])

    def test_opening_difference_row_is_flagged(self):
        out = compare(self._src(), {"available": False})
        temp = next(r for r in out["rows"] if r["key"] == "temporary_opening")
        self.assertTrue(temp["is_opening_difference"])

    def test_rows_in_trial_balance_order(self):
        src = self._src()
        keys = [r["key"] for r in compare(src, {"available": False})["rows"]]
        # ledger classes first (Asset before Equity), then the control/stock/diff rows.
        self.assertEqual(
            keys,
            ["class_Asset", "class_LiabEquity", "receivables", "payables",
             "stock", "temporary_opening"])


class TestCumulativeOpeningsFlag(unittest.TestCase):
    """The cumulative-openings heads-up is doubly gated: it fires only when a
    Receivables/Payables row diverges AND a prior Completed log for this company
    imported a DIFFERENT source file. Neither signal alone may trip it - a real
    single-export mismatch must keep the red alert. We drive the real method on a
    lightweight stand-in self with frappe.get_all stubbed.

    master_migrator imports frappe at module load, so this class is imported lazily
    and skips on a pure (no-frappe) runner; it runs under bench like the other
    frappe-tier tests."""

    @classmethod
    def setUpClass(cls):
        try:
            from tally_migrator.migration import master_migrator
        except ModuleNotFoundError as exc:
            raise unittest.SkipTest(f"frappe not importable: {exc}")
        cls.mm = master_migrator

    def _self(self):
        return SimpleNamespace(
            log=SimpleNamespace(name="TML-CURRENT"),
            config=SimpleNamespace(erpnext_company="Frappe Tech",
                                   source_file="NM.xml"))

    def _rec(self, *, diverged_key="receivables", available=True):
        # A matching row plus one diverging row keyed as requested.
        rows = [
            {"key": "class_Asset", "has_erpnext": True, "match": True},
            {"key": diverged_key, "has_erpnext": True, "match": False},
        ]
        return {"available": available, "rows": rows}

    def _run(self, rec, other_files):
        with mock.patch.object(self.mm.frappe, "get_all",
                               return_value=list(other_files)) as g:
            self.mm.MasterMigrator._flag_cumulative_openings(self._self(), rec)
        return rec, g

    def test_fires_when_party_diverges_and_other_export_present(self):
        rec, _ = self._run(self._rec(), ["Latest Data.xml", "Moooor.xml"])
        self.assertTrue(rec.get("cumulative_openings"))
        self.assertEqual(rec["other_exports"], ["Latest Data.xml", "Moooor.xml"])

    def test_silent_when_no_other_export(self):
        # Same file re-run only (or clean company): no different prior export.
        rec, _ = self._run(self._rec(), [])
        self.assertNotIn("cumulative_openings", rec)

    def test_silent_when_no_party_divergence(self):
        # Other exports exist, but only a non-party (ledger class) row diverges -
        # that is a genuine mismatch to alarm on, not a cumulative artefact.
        rec, g = self._run(self._rec(diverged_key="class_Asset"), ["Other.xml"])
        self.assertNotIn("cumulative_openings", rec)
        g.assert_not_called()   # short-circuits before querying logs

    def test_silent_when_reconciliation_unavailable(self):
        rec, g = self._run(self._rec(available=False), ["Other.xml"])
        self.assertNotIn("cumulative_openings", rec)
        g.assert_not_called()

    def test_payables_divergence_also_triggers(self):
        rec, _ = self._run(self._rec(diverged_key="payables"), ["Other.xml"])
        self.assertTrue(rec.get("cumulative_openings"))


if __name__ == "__main__":
    unittest.main()
