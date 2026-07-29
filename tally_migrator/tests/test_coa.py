"""Tests for Chart-of-Accounts extraction + classification. Pure, runs locally:
    python -m unittest tally_migrator.tests.test_coa
"""
import unittest

from tally_migrator.tally.extractors import TallyExtractor
from tally_migrator.tally.mappings import classify_group, is_system_ledger
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

    def test_reserved_match_is_case_and_whitespace_insensitive(self):
        # A chart typed in another case (or with stray spaces) must still classify -
        # otherwise account_type (Bank/Tax/Payable/...) is silently lost.
        self.assertEqual(classify_group("BANK ACCOUNTS")["account_type"], "Bank")
        self.assertEqual(classify_group("bank  accounts")["account_type"], "Bank")
        self.assertEqual(classify_group("DUTIES & TAXES")["account_type"], "Tax")
        # Aliases fold too ("Duties and Taxes" -> "Duties & Taxes").
        self.assertEqual(classify_group("duties and taxes")["account_type"], "Tax")
        # A genuinely custom group is still unclassified (must not false-match).
        self.assertIsNone(classify_group("RETAIL CUSTOMERS"))

    def test_system_ledger_case_insensitive(self):
        self.assertTrue(is_system_ledger("Profit & Loss A/c"))
        self.assertTrue(is_system_ledger("PROFIT & LOSS A/C"))
        self.assertFalse(is_system_ledger("Sales A/c"))


class TestCaseInsensitivePartyRoots(unittest.TestCase):
    """Regression: parties under a differently-cased/spaced Sundry Debtors / Creditors
    root must still classify as Customer / Supplier, not fall through to a ledger.
    (Reported symptom: an all-caps 'SUNDRY DEBTORS' chart imported every party as an
    Account.)"""

    def test_caps_debtor_root_nested(self):
        r = LedgerResolver(
            [_g("SUNDRY DEBTORS", ""), _g("DEBTORS FOR BROKERAGE", "SUNDRY DEBTORS")],
            [_l("GirishKumar", "DEBTORS FOR BROKERAGE")])
        self.assertEqual(r.kind_of("GirishKumar"), CUSTOMER)

    def test_caps_creditor_root(self):
        r = LedgerResolver([_g("SUNDRY CREDITORS", "")], [_l("Acme Freight", "SUNDRY CREDITORS")])
        self.assertEqual(r.kind_of("Acme Freight"), SUPPLIER)

    def test_whitespace_variant_root(self):
        r = LedgerResolver([_g("  Sundry   Debtors ", "")], [_l("WsCust", "  Sundry   Debtors ")])
        self.assertEqual(r.kind_of("WsCust"), CUSTOMER)

    def test_ledger_directly_under_root_not_emitted_as_group(self):
        # No group node for the root at all - a ledger directly under it must still be a
        # customer (the descendant set is seeded with the roots themselves).
        r = LedgerResolver([], [_l("DirectCust", "Sundry Debtors")])
        self.assertEqual(r.kind_of("DirectCust"), CUSTOMER)

    def test_created_name_keeps_original_casing(self):
        # Normalisation is for MATCHING only - the extracted account keeps Tally casing.
        src = _Src([_g("SUNDRY DEBTORS", ""), _g("DEBTORS FOR BROKERAGE", "SUNDRY DEBTORS")],
                   [_l("GirishKumar", "DEBTORS FOR BROKERAGE")])
        coa = TallyExtractor(src).extract_coa()
        names = {a.name for a in coa.accounts}
        self.assertIn("DEBTORS FOR BROKERAGE", names)   # original casing, not folded


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

    def test_custom_descendant_classified_as_party(self):
        # "Acme Corp" sits under "Retail Customers", a custom group nested under
        # Sundry Debtors - it must still resolve as a customer (deep descendants).
        self.assertEqual(self.r.kind_of("Acme Corp"), CUSTOMER)


class TestReservedNameFallback(unittest.TestCase):
    """A renamed reserved group keeps its rename-proof RESERVEDNAME attribute. The
    resolver must fall back to it so descendant ledgers still classify - proven
    against the real "Duties & Taxes" -> "Duties & Taxes Hello" Tally export.

    Shape from the export: the renamed group's PARENT is Current Liabilities, its
    display NAME is the new name, and RESERVEDNAME is still "Duties & Taxes". A
    ledger sits directly under it, and another under a *custom* sub-group of it.
    """

    def _groups(self):
        return [
            {"_name": "Current Liabilities", "Parent": "Primary", "ReservedName": "Current Liabilities"},
            # Reserved D&T renamed by the user; RESERVEDNAME survives.
            {"_name": "Duties & Taxes Hello", "Parent": "Current Liabilities",
             "ReservedName": "Duties & Taxes"},
            {"_name": "GST Sub", "Parent": "Duties & Taxes Hello", "ReservedName": ""},
            # A genuinely custom liability group - RESERVEDNAME empty, must stay ordinary.
            {"_name": "Statutory", "Parent": "Current Liabilities", "ReservedName": ""},
        ]

    def _ledgers(self):
        return [
            _l("CGST Output", "Duties & Taxes Hello"),
            _l("CGST Nested", "GST Sub"),
            _l("Ordinary Payable", "Statutory"),
        ]

    def test_renamed_reserved_group_still_classifies_as_tax(self):
        r = LedgerResolver(self._groups(), self._ledgers())
        for name in ("CGST Output", "CGST Nested"):
            t = r.resolve(name)
            self.assertEqual(t.account_type, "Tax", name)
            self.assertEqual(t.root_type, "Liability", name)

    def test_group_nature_of_renamed_group_is_reserved_source(self):
        r = LedgerResolver(self._groups(), self._ledgers())
        nat = r.group_nature("Duties & Taxes Hello")
        self.assertEqual(nat["account_type"], "Tax")
        self.assertEqual(nat["source"], "reserved")

    def test_custom_group_without_reserved_name_stays_ordinary(self):
        # The false-positive control: a real custom group must NOT be pulled into Tax.
        r = LedgerResolver(self._groups(), self._ledgers())
        t = r.resolve("Ordinary Payable")
        self.assertEqual(t.account_type, "")

    def test_display_name_still_wins_when_present(self):
        # Unrenamed reserved group (display NAME already classifies): behaviour
        # unchanged, reserved-name fallback never consulted.
        groups = [{"_name": "Duties & Taxes", "Parent": "Current Liabilities",
                   "ReservedName": "Duties & Taxes"}]
        r = LedgerResolver(groups, [_l("CGST", "Duties & Taxes")])
        self.assertEqual(r.resolve("CGST").account_type, "Tax")

    def test_missing_reserved_name_key_degrades_to_current_behaviour(self):
        # Groups without the ReservedName key at all (older callers / live source):
        # no crash, no fallback - exactly today's display-name-only behaviour.
        groups = [{"_name": "Renamed DT", "Parent": "Current Liabilities"}]
        r = LedgerResolver(groups, [_l("Some Ledger", "Renamed DT")])
        # "Renamed DT" is unknown and its parent Current Liabilities is reserved
        # (ordinary), so the ledger classifies as an ordinary liability - not Tax.
        self.assertEqual(r.resolve("Some Ledger").account_type, "")

    def test_renamed_party_roots_still_classify_parties(self):
        # Sundry Debtors / Sundry Creditors are reserved groups too. Renamed, their
        # RESERVEDNAME survives, so their ledgers (including nested ones) must still
        # come across as Customers / Suppliers, not ordinary accounts.
        groups = [
            {"_name": "My Customers", "Parent": "Primary", "ReservedName": "Sundry Debtors"},
            {"_name": "Retail", "Parent": "My Customers", "ReservedName": ""},
            {"_name": "Trade Payables", "Parent": "Primary", "ReservedName": "Sundry Creditors"},
        ]
        ledgers = [_l("Acme", "My Customers"), _l("Bob", "Retail"), _l("VendorX", "Trade Payables")]
        r = LedgerResolver(groups, ledgers)
        self.assertEqual(r.kind_of("Acme"), CUSTOMER)
        self.assertEqual(r.kind_of("Bob"), CUSTOMER)          # nested under renamed root
        self.assertEqual(r.kind_of("VendorX"), SUPPLIER)


def _gf(name, parent, is_revenue, is_deemed_positive):
    """A group carrying Tally's own nature flags (ISREVENUE / ISDEEMEDPOSITIVE)."""
    return {"_name": name, "Parent": parent,
            "IsRevenue": is_revenue, "IsDeemedPositive": is_deemed_positive}


class TestDerivedNature(unittest.TestCase):
    """A custom group with no reserved ancestor must derive its root_type from its
    own ISREVENUE/ISDEEMEDPOSITIVE flags, not blindly default to Asset."""

    # Top-level custom groups (empty parent) renamed away from Tally's reserved
    # names, exactly like the Bbb.xml fixture - so only their flags reveal the nature.
    GROUPS = [
        _gf("Trade Debtors", "", "No", "Yes"),       # balance-sheet, debit  → Asset
        _gf("Trade Creditors", "", "No", "No"),      # balance-sheet, credit → Liability
        _gf("Operating Income", "", "Yes", "No"),    # P&L, credit           → Income
        _gf("Office Expenses", "", "Yes", "Yes"),    # P&L, debit            → Expense
        _gf("Branch Office", "Office Expenses", "", ""),  # no own flags → inherit parent
        _g("Mystery Group", ""),                     # no flags, no ancestor → unknown
    ]

    def setUp(self):
        self.r = LedgerResolver(self.GROUPS, [])

    def test_credit_balance_sheet_is_liability(self):
        n = self.r.group_nature("Trade Creditors")
        self.assertEqual(n["root"], "Liability")
        self.assertEqual(n["source"], "derived")

    def test_debit_balance_sheet_is_asset(self):
        self.assertEqual(self.r.group_nature("Trade Debtors")["root"], "Asset")

    def test_credit_pnl_is_income(self):
        n = self.r.group_nature("Operating Income")
        self.assertEqual(n["root"], "Income")
        self.assertEqual(n["account_type"], "Income Account")

    def test_debit_pnl_is_expense(self):
        n = self.r.group_nature("Office Expenses")
        self.assertEqual(n["root"], "Expense")
        self.assertEqual(n["account_type"], "Expense Account")

    def test_flags_inherited_from_parent(self):
        # "Branch Office" carries no flags of its own; it must walk up to its parent.
        self.assertEqual(self.r.group_nature("Branch Office")["root"], "Expense")

    def test_unresolvable_is_unknown_defaulting_to_asset(self):
        n = self.r.group_nature("Mystery Group")
        self.assertEqual(n["source"], "unknown")
        self.assertEqual(n["root"], "Asset")  # still creatable, but flagged "--" in UI

    def test_reserved_still_wins_over_flags(self):
        # A reserved group must classify as reserved even though it also has flags.
        r = LedgerResolver([_gf("Bank Accounts", "Primary", "No", "Yes")], [])
        n = r.group_nature("Bank Accounts")
        self.assertEqual(n["source"], "reserved")
        self.assertEqual(n["account_type"], "Bank")


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
        # Parties migrate as Customers/Suppliers - they are not "excluded" losses.
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
        # Positive base amount → Credit (Tally's bare-sign convention).
        self.assertEqual(self._p("10.00$ = 800.00"), (800.0, "Cr"))

    def test_negative_is_dr(self):
        # Tally stores a Debit opening as a negative number.
        self.assertEqual(self._p("-1000"), (1000.0, "Dr"))

    def test_positive_is_cr(self):
        # ...and a Credit opening as positive.
        self.assertEqual(self._p("1000"), (1000.0, "Cr"))

    def test_real_export_anchor_signs(self):
        # Regression guard against a silent re-inversion of the sign convention,
        # pinned to real-export ground truth (see _parse_opening docstring):
        #   Capital Account exports POSITIVE and is always a Credit;
        #   a bank/asset opening exports NEGATIVE and is a Debit.
        self.assertEqual(self._p("100000.00"), (100000.0, "Cr"))   # Capital → Cr
        self.assertEqual(self._p("-10200.00"), (10200.0, "Dr"))    # HDFC Bank → Dr

    def test_blank_and_zero(self):
        self.assertEqual(self._p(""), (0.0, ""))
        self.assertEqual(self._p("0.00"), (0.0, ""))


if __name__ == "__main__":
    unittest.main()
