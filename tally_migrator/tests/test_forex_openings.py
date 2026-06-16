"""
Phase 6 tests: foreign-currency party openings (Option B - true multi-currency AR/AP).

A forex party is detected from the currency symbol embedded in its opening balance
(Tally exports no CurrencyName tag), resolved to an ISO via the CURRENCY masters.
Its opening posts as one invoice in the foreign currency (conversion_rate derived
from Tally's stated base / foreign amount, so the base reconciles exactly) against a
currency-denominated receivable/payable account.
"""
import types
import unittest
from unittest import mock

from tally_migrator.tally.file_source import FileTallySource
from tally_migrator.tally.extractors import TallyExtractor
from tally_migrator.erpnext.importers.openings import PartyOpeningImporter
from tally_migrator.erpnext.importers import ImportResult


class TestParseForexOpening(unittest.TestCase):
    def test_rich_and_legacy_forms(self):
        P = TallyExtractor._parse_forex_opening
        self.assertEqual(P("-$8500.00 @ ₹ 83/$ = -₹ 705500.00"), (8500.0, "$", 705500.0, "Dr"))
        self.assertEqual(P("10.00$ = 800.00"), (10.0, "$", 800.0, "Cr"))
        self.assertEqual(P("-2500.00"), (0.0, "", 0.0, ""))      # no '=' -> not forex
        self.assertEqual(P(""), (0.0, "", 0.0, ""))


class TestCurrencyIsoExtraction(unittest.TestCase):
    def test_symbol_in_opening_resolves_to_iso(self):
        xml = (
            '<ENVELOPE><BODY><IMPORTDATA><REQUESTDATA>'
            '<TALLYMESSAGE><CURRENCY NAME="$"><NAME>$</NAME>'
            '<ISOCURRENCYCODE>USD</ISOCURRENCYCODE></CURRENCY></TALLYMESSAGE>'
            '<TALLYMESSAGE><LEDGER NAME="FX Co"><NAME>FX Co</NAME>'
            '<PARENT>Sundry Debtors</PARENT>'
            '<OPENINGBALANCE>-$8500.00 @ ₹ 83/$ = -₹ 705500.00</OPENINGBALANCE>'
            '</LEDGER></TALLYMESSAGE>'
            '<TALLYMESSAGE><LEDGER NAME="Local Co"><NAME>Local Co</NAME>'
            '<PARENT>Sundry Debtors</PARENT>'
            '<OPENINGBALANCE>-50000.00</OPENINGBALANCE></LEDGER></TALLYMESSAGE>'
            '</REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>'
        )
        masters = TallyExtractor(FileTallySource(xml)).extract_all()
        by_name = {c["_name"]: c for c in masters.customers}
        self.assertEqual(by_name["FX Co"].get("CurrencyISO"), "USD")
        self.assertIsNone(by_name["Local Co"].get("CurrencyISO"))   # domestic untouched


class TestForexInvoiceDict(unittest.TestCase):
    def _imp(self):
        imp = PartyOpeningImporter.__new__(PartyOpeningImporter)
        imp.company, imp.abbr, imp.posting_date = "Frappe Tech", "FT", "2026-04-01"
        imp._stock_uom = lambda: "Nos"
        imp._temp_account = lambda: "Temporary Opening - FT"
        imp._cost_center = lambda: "Main - FT"
        return imp

    def test_customer_invoice(self):
        d = self._imp()._forex_invoice_dict(
            "Customer", "FX Co", 8500.0, "USD", 83.0, "Debtors USD - FT", "mk")
        self.assertEqual(d["doctype"], "Sales Invoice")
        self.assertEqual((d["currency"], d["conversion_rate"], d["debit_to"]),
                         ("USD", 83.0, "Debtors USD - FT"))
        self.assertEqual(d["items"][0]["rate"], 8500.0)             # foreign amount
        self.assertEqual(d["items"][0]["income_account"], "Temporary Opening - FT")
        self.assertEqual(d["is_opening"], "Yes")

    def test_supplier_invoice_uses_credit_to(self):
        d = self._imp()._forex_invoice_dict(
            "Supplier", "FX Sup", 6000.0, "USD", 83.0, "Creditors USD - FT", "mk")
        self.assertEqual(d["doctype"], "Purchase Invoice")
        self.assertEqual(d["credit_to"], "Creditors USD - FT")
        self.assertEqual(d["items"][0]["expense_account"], "Temporary Opening - FT")
        self.assertEqual(d["bill_no"], "Opening")


class TestEnsureCurrencyAccount(unittest.TestCase):
    def _imp(self):
        imp = PartyOpeningImporter.__new__(PartyOpeningImporter)
        imp.company, imp.abbr = "Frappe Tech", "FT"
        imp._party_account = lambda pt: "Debtors - FT"
        return imp

    def test_reuses_existing(self):
        with mock.patch("frappe.db.exists", return_value=True), \
                mock.patch("frappe.get_doc") as gd:
            name = self._imp()._ensure_currency_account("Customer", "USD")
        self.assertEqual(name, "Debtors USD - FT")
        gd.assert_not_called()

    def test_creates_with_currency_and_type(self):
        captured = {}

        def fake_get_doc(d):
            captured.update(d)
            return types.SimpleNamespace(name="Debtors USD - FT", insert=lambda **k: None)
        with mock.patch("frappe.db.exists", return_value=False), \
                mock.patch("frappe.db.get_value", return_value="Accounts Receivable - FT"), \
                mock.patch("frappe.get_doc", side_effect=fake_get_doc), \
                mock.patch("frappe.db.commit"):
            self._imp()._ensure_currency_account("Customer", "USD")
        self.assertEqual(captured["account_currency"], "USD")
        self.assertEqual(captured["account_type"], "Receivable")
        self.assertEqual(captured["parent_account"], "Accounts Receivable - FT")


class TestEmitForexGuards(unittest.TestCase):
    def _imp(self):
        imp = PartyOpeningImporter.__new__(PartyOpeningImporter)
        imp.company, imp.abbr, imp.posting_date = "Frappe Tech", "FT", "2026-04-01"
        imp._company_currency = lambda: "INR"
        return imp

    def test_skips_when_no_iso(self):
        res = ImportResult("Opening Invoice")
        self._imp()._emit_forex("Customer", "FX", "FX", {"CurrencyISO": ""}, [], set(), res)
        self.assertEqual(res.created, 0)
        self.assertTrue(any("couldn't resolve" in w["reason"] for w in res.warnings))

    def test_skips_forex_advance(self):
        res = ImportResult("Opening Invoice")
        # A customer credit (Cr) opening is an advance -> not migrated (v1)
        rec = {"CurrencyISO": "USD", "OpeningBalance": "$1000.00 @ ₹ 83/$ = ₹ 83000.00"}
        self._imp()._emit_forex("Customer", "FX", "FX", rec, [], set(), res)
        self.assertEqual(res.created, 0)
        self.assertTrue(any("advance" in w["reason"] for w in res.warnings))


class TestReconciliationIncludesForexAccounts(unittest.TestCase):
    """The reconciliation receivables/payables figure must sum opening postings across
    ALL receivable/payable accounts, so the per-currency forex control accounts
    (Debtors USD / Creditors USD) are not missed - otherwise forex migrations show a
    false variance."""

    def test_opening_account_balance_accepts_account_list(self):
        from tally_migrator.migration import reconciliation as R
        captured = {}

        import frappe

        def fake_get_all(doctype, filters=None, fields=None, **kw):
            captured.setdefault("filters", []).append(filters)
            # one opening row of 705500 Dr on the (forex) account
            return [frappe._dict(debit=705500.0, credit=0.0)] if filters.get("is_opening") else []
        with mock.patch("frappe.get_all", side_effect=fake_get_all):
            out = R._opening_account_balance("FT", ["Debtors - FT", "Debtors USD - FT"])
        self.assertEqual(out, {"amount": 705500.0, "dr_cr": "Dr"})
        # the account filter is an 'in' over the full list, not a single account
        self.assertEqual(captured["filters"][0]["account"], ["in", ["Debtors - FT", "Debtors USD - FT"]])


if __name__ == "__main__":
    unittest.main()
