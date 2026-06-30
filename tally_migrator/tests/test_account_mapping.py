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

    def test_opening_plug_matches_reconciliation_including_stock(self):
        """The Review-step plug and the Log's reconciliation Temporary Opening are
        the same quantity and must agree: both are the contra of every opening
        posting, INCLUDING opening stock (which posts to Temporary Opening too).
        Regression - an earlier _opening_plug omitted stock and disagreed with the
        Log, so the two screens showed different numbers for the same file."""
        from tally_migrator.tally.extractors import TallyExtractor
        from tally_migrator.migration.reconciliation import source_totals

        class _StockSrc(_Src):
            def __init__(self, groups, ledgers, items):
                super().__init__(groups, ledgers)
                self._data["Stock Item"] = items

        items = [{"_name": "Widget", "OpeningBalance": "10 Nos",
                  "OpeningRate": "100.00/Nos"}]            # 10 x 100 = 1000 Dr
        src = _StockSrc(GROUPS, LEDGERS, items)
        plug = account_mapping(src)["opening"]

        ex = TallyExtractor(src)
        recon = source_totals(ex.extract_coa(), ex.extract_all())["temporary_opening"]
        # Same number on both screens - the whole point of the fix.
        self.assertEqual(plug["temporary_opening_plug"], recon["amount"])
        self.assertEqual(plug["plug_dr_cr"], recon["dr_cr"])
        # And stock is actually in it: 52000 + 15000 (accounts + party) + 1000 stock
        # = 68000 net Dr, so the balancing Temporary Opening line is 68000 Cr.
        self.assertEqual(plug["temporary_opening_plug"], 68000.0)
        self.assertEqual(plug["plug_dr_cr"], "Cr")

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

    def test_natural_side_advance_flagged_bill_counts_as_invoice(self):
        # Regression: a bill on the party's NATURAL side that Tally also tags
        # ISADVANCE must be previewed as an outstanding invoice, NOT an advance -
        # that is exactly what PartyOpeningImporter._classify posts (it routes by side
        # and ignores ISADVANCE). The Step-4 screen must report what migrates, so its
        # advance/invoice split has to match the importer. The old preview forced
        # ISADVANCE to 'advance' and over-counted advances versus what actually posted.
        from tally_migrator.tally.file_source import FileTallySource

        xml = (
            '<ENVELOPE><BODY><IMPORTDATA><REQUESTDATA>'
            '<TALLYMESSAGE><GROUP NAME="Sundry Debtors"><NAME>Sundry Debtors</NAME>'
            '<PARENT>Primary</PARENT></GROUP></TALLYMESSAGE>'
            '<TALLYMESSAGE><LEDGER NAME="Acme Corp"><NAME>Acme Corp</NAME>'
            '<PARENT>Sundry Debtors</PARENT><OPENINGBALANCE>-5000.00</OPENINGBALANCE>'
            '<BILLALLOCATIONS.LIST><NAME>INV-1</NAME><BILLDATE>20260401</BILLDATE>'
            '<ISADVANCE>Yes</ISADVANCE><OPENINGBALANCE>-5000.00</OPENINGBALANCE>'
            '</BILLALLOCATIONS.LIST>'
            '</LEDGER></TALLYMESSAGE>'
            '</REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>'
        )
        p = account_mapping(FileTallySource(xml))["party_openings"]
        self.assertEqual(p["invoices"], 1)     # the natural-side bill -> invoice
        self.assertEqual(p["advances"], 0)     # NOT forced to an advance by ISADVANCE
        row = next(r for r in p["parties_list"] if r["name"] == "Acme Corp")
        self.assertEqual(row["invoices"], 1)
        self.assertEqual(row["advances"], 0)
        self.assertEqual(row["documents"], 1)  # net ties to ledger, so no extra plug

    def test_balanced_book_reads_clean(self):
        ledgers = [
            _l("HDFC Bank", "Bank Accounts", "50000.00 Dr"),
            _l("Share Capital", "Bank Accounts", "50000.00 Cr"),
        ]
        m = account_mapping(_Src(GROUPS, ledgers))
        self.assertTrue(m["opening"]["clean"])
        self.assertEqual(m["opening"]["plug_dr_cr"], "")

    def test_foreign_currency_party_is_posted_and_counted(self):
        # A forex party is detected by the currency SYMBOL in its opening string (which
        # resolves to CurrencyISO), NOT by CurrencyName (a real export omits it on a
        # forex party). It is posted in its own currency, so the preview counts it and
        # reports `foreign`, rather than skipping it. No per-bill detail here → 1 lump.
        from tally_migrator.tally.file_source import FileTallySource

        xml = (
            '<ENVELOPE><BODY><IMPORTDATA><REQUESTDATA>'
            '<TALLYMESSAGE><GROUP NAME="Sundry Debtors"><NAME>Sundry Debtors</NAME>'
            '<PARENT>Primary</PARENT></GROUP></TALLYMESSAGE>'
            '<TALLYMESSAGE><CURRENCY NAME="$"><NAME>$</NAME>'
            '<ISOCURRENCYCODE>USD</ISOCURRENCYCODE></CURRENCY></TALLYMESSAGE>'
            '<TALLYMESSAGE><LEDGER NAME="Acme India"><NAME>Acme India</NAME>'
            '<PARENT>Sundry Debtors</PARENT><OPENINGBALANCE>-15000.00</OPENINGBALANCE>'
            '</LEDGER></TALLYMESSAGE>'
            '<TALLYMESSAGE><LEDGER NAME="Foreign Buyer LLC"><NAME>Foreign Buyer LLC</NAME>'
            '<PARENT>Sundry Debtors</PARENT>'
            '<OPENINGBALANCE>-$9000.00 @ ₹ 83/$ = -₹ 747000.00</OPENINGBALANCE>'
            '</LEDGER></TALLYMESSAGE>'
            '</REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>'
        )
        p = account_mapping(FileTallySource(xml))["party_openings"]
        self.assertEqual(p["foreign"], 1)
        self.assertEqual(p["parties"], 2)              # BOTH parties counted now
        row = next(r for r in p["parties_list"] if r["name"] == "Foreign Buyer LLC")
        self.assertTrue(row["foreign"])
        self.assertEqual(row["documents"], 1)          # no per-bill detail → one invoice

    def test_foreign_party_with_per_bill_amounts_splits_in_preview(self):
        # When the export carries per-bill foreign amounts, the preview counts one
        # invoice per bill (mirroring the importer's per-bill forex split).
        from tally_migrator.tally.file_source import FileTallySource

        def _bill(nm, ob):
            return (f'<BILLALLOCATIONS.LIST><NAME>{nm}</NAME><BILLDATE>20260401</BILLDATE>'
                    f'<ISADVANCE>No</ISADVANCE><OPENINGBALANCE>{ob}</OPENINGBALANCE>'
                    '</BILLALLOCATIONS.LIST>')
        xml = (
            '<ENVELOPE><BODY><IMPORTDATA><REQUESTDATA>'
            '<TALLYMESSAGE><GROUP NAME="Sundry Debtors"><NAME>Sundry Debtors</NAME>'
            '<PARENT>Primary</PARENT></GROUP></TALLYMESSAGE>'
            '<TALLYMESSAGE><CURRENCY NAME="$"><NAME>$</NAME>'
            '<ISOCURRENCYCODE>USD</ISOCURRENCYCODE></CURRENCY></TALLYMESSAGE>'
            '<TALLYMESSAGE><LEDGER NAME="Globex USD"><NAME>Globex USD</NAME>'
            '<PARENT>Sundry Debtors</PARENT>'
            '<OPENINGBALANCE>-$1000.00 @ ₹ 83/$ = -₹ 83000.00</OPENINGBALANCE>'
            + _bill("USD-1", "-$600.00 @ ₹ 83/$ = -₹ 49800.00")
            + _bill("USD-2", "-$400.00 @ ₹ 83/$ = -₹ 33200.00")
            + '</LEDGER></TALLYMESSAGE>'
            '</REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>'
        )
        p = account_mapping(FileTallySource(xml))["party_openings"]
        self.assertEqual(p["foreign"], 1)
        row = next(r for r in p["parties_list"] if r["name"] == "Globex USD")
        self.assertEqual(row["invoices"], 2)           # one per bill, not one lump
        self.assertEqual(row["documents"], 2)


if __name__ == "__main__":
    unittest.main()
