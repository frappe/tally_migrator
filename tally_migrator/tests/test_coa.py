"""Tests for Chart-of-Accounts extraction + classification. Pure, runs locally:
    python -m unittest tally_migrator.tests.test_coa
"""
import unittest

from tally_migrator.tally.extractors import TallyExtractor
from tally_migrator.tally.mappings import classify_group
from tally_migrator.tally.resolver import LedgerResolver, CUSTOMER, SUPPLIER, ACCOUNT


class _Src:
    """Stub Tally source returning canned collections."""
    def __init__(self, groups, ledgers, centres=None):
        self._data = {"Group": groups, "Ledger": ledgers, "Cost Centre": centres or []}

    def ping(self):
        return True

    def get_collection(self, obj_type, fields, tag_map=None):
        return self._data.get(obj_type, [])


def _g(name, parent):
    return {"_name": name, "Parent": parent}


def _l(name, parent, ob=""):
    return {"_name": name, "Parent": parent, "OpeningBalance": ob}


GROUPS = [
    _g("Current Assets", "Primary"),
    _g("Bank Accounts", "Primary"),
    _g("Indirect Expenses", "Primary"),
    _g("Sundry Debtors", "Current Assets"),
    _g("Retail Customers", "Sundry Debtors"),   # custom group in the party tree
    _g("Telecom Expenses", "Indirect Expenses"),  # custom expense group
]
LEDGERS = [
    _l("Acme Corp", "Retail Customers", "15000.00 Dr"),   # customer → not an account
    _l("HDFC Bank", "Bank Accounts", "50000.00 Dr"),      # bank account
    _l("Phone Bill", "Telecom Expenses"),                 # expense account
    _l("Profit & Loss A/c", "Primary"),                   # system ledger → skipped
]
CENTRES = [_g("Head Office", ""), _g("Sales Dept", "Head Office")]


class TestClassify(unittest.TestCase):
    def test_reserved_group(self):
        self.assertEqual(classify_group("Bank Accounts")["root"], "Asset")
        self.assertEqual(classify_group("Bank Accounts")["account_type"], "Bank")

    def test_alias_resolves(self):
        self.assertEqual(classify_group("Duties and Taxes")["account_type"], "Tax")

    def test_custom_group_is_none(self):
        self.assertIsNone(classify_group("Retail Customers"))


class TestResolver(unittest.TestCase):
    def setUp(self):
        self.r = LedgerResolver(GROUPS, LEDGERS)

    def test_party_classification(self):
        self.assertEqual(self.r.kind_of("Acme Corp"), CUSTOMER)

    def test_account_classification(self):
        t = self.r.resolve("HDFC Bank")
        self.assertEqual(t.kind, ACCOUNT)
        self.assertEqual(t.root_type, "Asset")
        self.assertEqual(t.account_type, "Bank")

    def test_group_nature_walks_to_reserved_ancestor(self):
        self.assertEqual(self.r.group_nature("Telecom Expenses")["root"], "Expense")

    def test_party_groups_include_custom_descendants(self):
        self.assertIn("Retail Customers", self.r.party_groups)


class TestBuildCOA(unittest.TestCase):
    def setUp(self):
        self.coa = TallyExtractor(_Src(GROUPS, LEDGERS, CENTRES)).extract_coa()
        self.by_name = {a.name: a for a in self.coa.accounts}

    def test_counts(self):
        self.assertEqual(self.coa.summary["account_groups"], 6)
        self.assertEqual(self.coa.summary["ledger_accounts"], 2)  # HDFC + Phone Bill
        self.assertEqual(self.coa.summary["cost_centres"], 2)

    def test_parties_excluded(self):
        self.assertNotIn("Acme Corp", self.by_name)

    def test_system_ledger_excluded(self):
        self.assertNotIn("Profit & Loss A/c", self.by_name)

    def test_ledger_account_nature_and_opening(self):
        hdfc = self.by_name["HDFC Bank"]
        self.assertFalse(hdfc.is_group)
        self.assertEqual(hdfc.root_type, "Asset")
        self.assertEqual(hdfc.account_type, "Bank")
        self.assertEqual(hdfc.opening_balance, 50000.0)
        self.assertEqual(hdfc.opening_dr_cr, "Dr")

    def test_reserved_flagging(self):
        self.assertTrue(self.by_name["Bank Accounts"].is_reserved)
        self.assertFalse(self.by_name["Retail Customers"].is_reserved)

    def test_primary_parent_normalised(self):
        self.assertEqual(self.by_name["Current Assets"].parent, "")  # "Primary" -> ""

    def test_cost_centre_parent(self):
        sales = next(c for c in self.coa.cost_centres if c.name == "Sales Dept")
        self.assertEqual(sales.parent, "Head Office")

    def test_system_ledger_recorded_as_excluded(self):
        # Profit & Loss A/c is skipped from the COA but must be traceable, not lost.
        names = [e["name"] for e in self.coa.excluded]
        self.assertIn("Profit & Loss A/c", names)
        self.assertEqual(self.coa.summary["excluded_ledgers"], 1)

    def test_parties_not_in_excluded(self):
        # Parties migrate as Customers/Suppliers — they are not "excluded" losses.
        names = [e["name"] for e in self.coa.excluded]
        self.assertNotIn("Acme Corp", names)


class TestParseOpening(unittest.TestCase):
    def _p(self, raw):
        return TallyExtractor._parse_opening(raw)

    def test_dr_suffix(self):
        self.assertEqual(self._p("15000.00 Dr"), (15000.0, "Dr"))

    def test_cr_suffix(self):
        self.assertEqual(self._p("45000.00 Cr"), (45000.0, "Cr"))

    def test_multicurrency_takes_base(self):
        self.assertEqual(self._p("10.00$ = 800.00"), (800.0, "Dr"))

    def test_negative_is_cr(self):
        self.assertEqual(self._p("-1000"), (1000.0, "Cr"))

    def test_blank_and_zero(self):
        self.assertEqual(self._p(""), (0.0, ""))
        self.assertEqual(self._p("0.00"), (0.0, ""))


if __name__ == "__main__":
    unittest.main()
