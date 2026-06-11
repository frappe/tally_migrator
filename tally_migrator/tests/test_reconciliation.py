"""Unit tests for the read-only reconciliation trial balance (pure parts, no DB)."""
import unittest

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
            _acct("Rent", "Expense", 2000, "Dr"),
        ])
        cls = _classes(source_totals(coa, _masters()))
        self.assertEqual(cls["Asset"], {"amount": 15200.0, "dr_cr": "Dr"})
        self.assertEqual(cls["Equity"], {"amount": 100000.0, "dr_cr": "Cr"})
        self.assertEqual(cls["Expense"], {"amount": 2000.0, "dr_cr": "Dr"})

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
            ["class_Asset", "class_Equity", "receivables", "payables",
             "stock", "temporary_opening"])


if __name__ == "__main__":
    unittest.main()
