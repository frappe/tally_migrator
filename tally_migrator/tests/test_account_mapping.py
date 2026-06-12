"""Tests for the Step-4 accounts-mapping preview. Pure, runs locally:
    python -m unittest tally_migrator.tests.test_account_mapping
"""
import unittest

from tally_migrator.migration.account_mapping import account_mapping


class _Src:
    """Stub Tally source returning canned collections (mirrors test_coa)."""
    def __init__(self, groups, ledgers):
        self._data = {"Group": groups, "Ledger": ledgers, "Cost Centre": []}

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
    _g("Telecom Expenses", "Indirect Expenses"),   # custom, reserved ancestor
    _g("Mystery", "Primary"),                       # custom, NO reserved ancestor
]
LEDGERS = [
    _l("Acme Corp", "Sundry Debtors", "15000.00 Dr"),  # customer → not an account
    _l("HDFC Bank", "Bank Accounts", "50000.00 Dr"),   # bank account, confident
    _l("Phone Bill", "Telecom Expenses"),              # expense account, confident
    _l("Weird Ledger", "Mystery", "2000.00 Dr"),       # inferred (fallback nature)
]


class TestAccountMapping(unittest.TestCase):
    def setUp(self):
        self.m = account_mapping(_Src(GROUPS, LEDGERS))

    def test_counts_exclude_parties_and_groups(self):
        # HDFC, Phone Bill, Weird Ledger - the customer is not an account.
        self.assertEqual(self.m["total_accounts"], 3)

    def test_inferred_is_the_fallback_row_only(self):
        self.assertEqual(self.m["inferred_count"], 1)
        names = [r["name"] for r in self.m["inferred"]]
        self.assertEqual(names, ["Weird Ledger"])

    def test_confident_rows_are_not_flagged(self):
        flagged = {r["name"]: r["inferred"] for g in self.m["groups"] for r in g["accounts"]}
        self.assertFalse(flagged["HDFC Bank"])
        self.assertFalse(flagged["Phone Bill"])
        self.assertTrue(flagged["Weird Ledger"])

    def test_grouped_by_root_type_with_subtotals(self):
        roots = {g["root_type"] for g in self.m["groups"]}
        self.assertIn("Asset", roots)
        self.assertIn("Expense", roots)
        asset = next(g for g in self.m["groups"] if g["root_type"] == "Asset")
        # HDFC (50000 Dr) + Weird (2000 Dr) both land under Asset.
        self.assertEqual(asset["subtotal_dr"], 52000.0)

    def test_opening_plug_spans_accounts_and_parties(self):
        # 50000 + 2000 (accounts, Dr) + 15000 (customer, Dr) = 67000 net Dr.
        plug = self.m["opening"]
        self.assertEqual(plug["temporary_opening_plug"], 67000.0)
        self.assertEqual(plug["plug_dr_cr"], "Cr")   # balancing line opposes the net
        self.assertFalse(plug["clean"])

    def test_party_openings_list_per_party(self):
        # The collapsed "all parties" list carries one row per party with an
        # opening, with the ledger amount/side - Acme Corp is the lone customer.
        plist = self.m["party_openings"]["parties_list"]
        acme = next(r for r in plist if r["name"] == "Acme Corp")
        self.assertEqual(acme["party_type"], "Customer")
        self.assertEqual(acme["amount"], 15000.0)
        self.assertEqual(acme["dr_cr"], "Dr")
        # No bill detail in this fixture, so it posts a single lump opening document.
        self.assertEqual(acme["documents"], 1)

    def test_balanced_book_reads_clean(self):
        ledgers = [
            _l("HDFC Bank", "Bank Accounts", "50000.00 Dr"),
            _l("Share Capital", "Bank Accounts", "50000.00 Cr"),
        ]
        m = account_mapping(_Src(GROUPS, ledgers))
        self.assertTrue(m["opening"]["clean"])
        self.assertEqual(m["opening"]["plug_dr_cr"], "")

    def test_foreign_currency_party_is_skipped_from_preview(self):
        # The base currency is the modal ledger CurrencyName (INR here); a party in a
        # different currency is skipped at import, so the preview skips it too and
        # reports it as foreign_skipped rather than counting its documents.
        def _c(name, ob, ccy):
            return {"_name": name, "Parent": "Sundry Debtors",
                    "OpeningBalance": ob, "CurrencyName": ccy}
        ledgers = [
            _c("Acme India", "15000.00 Dr", "INR"),
            _c("Bharat Traders", "8000.00 Dr", "INR"),
            _c("Foreign Buyer LLC", "9000.00 Dr", "USD"),
        ]
        p = account_mapping(_Src(GROUPS, ledgers))["party_openings"]
        self.assertEqual(p["foreign_skipped"], 1)
        self.assertEqual(p["parties"], 2)        # the two INR parties only
        names = [r["name"] for r in p["parties_list"]]
        self.assertNotIn("Foreign Buyer LLC", names)


if __name__ == "__main__":
    unittest.main()
