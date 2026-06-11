"""Unit tests for FileTallySource (offline Tally masters XML parsing).

No Tally connection and no Frappe site required - exercises the parser and its
interop with TallyExtractor directly.
"""
import unittest

from tally_migrator.tally.file_source import (
    FileTallySource, decode_tally_bytes, sanitize_tally_xml,
)
from tally_migrator.tally.extractors import (
    TallyExtractor, LEDGER_FIELDS, LEDGER_TAGS, ITEM_FIELDS, ITEM_TAGS,
)


# A trimmed but structurally faithful Tally Prime "Export Masters (XML)" file.
SAMPLE_XML = """<ENVELOPE>
  <BODY><IMPORTDATA><REQUESTDATA>
    <TALLYMESSAGE><GROUP NAME="Sundry Debtors"><PARENT>Primary</PARENT></GROUP></TALLYMESSAGE>
    <TALLYMESSAGE><GROUP NAME="Retail Debtors"><PARENT>Sundry Debtors</PARENT></GROUP></TALLYMESSAGE>
    <TALLYMESSAGE><GROUP NAME="Sundry Creditors"><PARENT>Primary</PARENT></GROUP></TALLYMESSAGE>
    <TALLYMESSAGE>
      <LEDGER NAME="Customer A"><PARENT>Sundry Debtors</PARENT>
        <OPENINGBALANCE>1500.00</OPENINGBALANCE></LEDGER>
    </TALLYMESSAGE>
    <TALLYMESSAGE><LEDGER NAME="Customer B"><PARENT>Retail Debtors</PARENT></LEDGER></TALLYMESSAGE>
    <TALLYMESSAGE><LEDGER NAME="Supplier X"><PARENT>Sundry Creditors</PARENT></LEDGER></TALLYMESSAGE>
    <TALLYMESSAGE>
      <STOCKITEM NAME="Widget"><PARENT>All Items</PARENT><BASEUNITS>Nos</BASEUNITS></STOCKITEM>
    </TALLYMESSAGE>
    <TALLYMESSAGE><GODOWN NAME="Main Store"><PARENT/></GODOWN></TALLYMESSAGE>
  </REQUESTDATA></IMPORTDATA></BODY>
</ENVELOPE>"""


class TestFileTallySource(unittest.TestCase):
    def setUp(self):
        self.source = FileTallySource(SAMPLE_XML)

    def test_ping_always_true(self):
        self.assertTrue(self.source.ping())

    def test_get_collection_reads_name_attr_and_fields(self):
        ledgers = self.source.get_collection("Ledger", ["Parent", "OpeningBalance"])
        by_name = {l["_name"]: l for l in ledgers}
        self.assertEqual(by_name["Customer A"]["Parent"], "Sundry Debtors")
        self.assertEqual(by_name["Customer A"]["OpeningBalance"], "1500.00")

    def test_stock_item_tag_with_space_in_objtype(self):
        items = self.source.get_collection("Stock Item", ["BaseUnits"])
        self.assertEqual(items[0]["_name"], "Widget")
        self.assertEqual(items[0]["BaseUnits"], "Nos")

    def test_missing_field_is_empty_string(self):
        items = self.source.get_collection("Stock Item", ["HSNCode"])
        self.assertEqual(items[0]["HSNCode"], "")

    def test_interops_with_extractor(self):
        """The whole point: the extractor can't tell file from live client."""
        masters = TallyExtractor(self.source).extract_all()
        self.assertEqual({c["_name"] for c in masters.customers}, {"Customer A", "Customer B"})
        self.assertEqual({s["_name"] for s in masters.suppliers}, {"Supplier X"})
        self.assertEqual(len(masters.items), 1)
        self.assertEqual(len(masters.warehouses), 1)

    def test_invalid_xml_raises(self):
        with self.assertRaises(Exception):
            FileTallySource("<ENVELOPE><not-closed>")

    def test_approx_record_count_sums_kept_records(self):
        # 3 groups + 3 ledgers + 1 stock item + 1 godown in SAMPLE_XML.
        self.assertEqual(self.source.approx_record_count(), 8)

    def test_scale_many_records_parse_and_count(self):
        """Smoke test that the streaming parser handles a large record count and
        the per-tag buckets stay exact (guards the iterparse/root.clear path)."""
        n = 5000
        msgs = "".join(
            f'<TALLYMESSAGE><LEDGER NAME="C{i}"><PARENT>Sundry Debtors</PARENT></LEDGER></TALLYMESSAGE>'
            for i in range(n)
        )
        src = FileTallySource(f"<ENVELOPE><BODY>{msgs}</BODY></ENVELOPE>")
        self.assertEqual(src.approx_record_count(), n)
        ledgers = src.get_collection("Ledger", ["Parent"])
        self.assertEqual(len(ledgers), n)
        self.assertEqual(ledgers[-1]["_name"], f"C{n - 1}")
        self.assertEqual(ledgers[0]["Parent"], "Sundry Debtors")

    def test_streaming_ignores_non_master_chrome(self):
        # A COMPANY header and voucher data must not inflate the record buckets.
        xml = ("<ENVELOPE><BODY>"
               "<COMPANY><NAME>Acme</NAME></COMPANY>"
               "<TALLYMESSAGE><VOUCHER><AMOUNT>1</AMOUNT></VOUCHER></TALLYMESSAGE>"
               "<TALLYMESSAGE><LEDGER NAME=\"L1\"><PARENT>Sundry Debtors</PARENT></LEDGER></TALLYMESSAGE>"
               "</BODY></ENVELOPE>")
        src = FileTallySource(xml)
        self.assertEqual(src.approx_record_count(), 1)
        self.assertEqual(src.get_collection("Ledger", ["Parent"])[0]["Parent"], "Sundry Debtors")


# Two records: one in real Tally tags (state <LEDSTATENAME>, email <EMAIL>,
# multi-line <ADDRESS.LIST>, price/cost <STANDARDPRICELIST.LIST> revision lists),
# and one using the OLD invented flat tags (<LEDGERSTATE>/<LEDGEREMAIL>/flat
# <ADDRESS>/<STANDARDPRICE>) which the parser must now IGNORE - only real Tally
# tags are read.
REAL_TALLY_XML = """<ENVELOPE>
  <BODY><IMPORTDATA><REQUESTDATA>
    <TALLYMESSAGE>
      <LEDGER NAME="Invented Tag Co"><PARENT>Sundry Debtors</PARENT>
        <LEDGERSTATE>Gujarat</LEDGERSTATE>
        <LEDGEREMAIL>flat@example.com</LEDGEREMAIL>
        <ADDRESS>12 Flat Road</ADDRESS></LEDGER>
    </TALLYMESSAGE>
    <TALLYMESSAGE>
      <LEDGER NAME="Real Tally Co"><PARENT>Sundry Debtors</PARENT>
        <LEDSTATENAME>Karnataka</LEDSTATENAME>
        <EMAIL>real@example.com</EMAIL>
        <ADDRESS.LIST TYPE="String">
          <ADDRESS>Door No 5</ADDRESS>
          <ADDRESS>MG Road</ADDRESS>
          <ADDRESS>Bengaluru</ADDRESS>
        </ADDRESS.LIST></LEDGER>
    </TALLYMESSAGE>
    <TALLYMESSAGE>
      <STOCKITEM NAME="Gadget"><PARENT>All Items</PARENT><BASEUNITS>Nos</BASEUNITS>
        <STANDARDPRICELIST.LIST><DATE>20240101</DATE><RATE>99.50</RATE></STANDARDPRICELIST.LIST>
        <STANDARDCOSTLIST.LIST><DATE>20240101</DATE><RATE>72.00</RATE></STANDARDCOSTLIST.LIST></STOCKITEM>
    </TALLYMESSAGE>
  </REQUESTDATA></IMPORTDATA></BODY>
</ENVELOPE>"""


class TestRealTallyTags(unittest.TestCase):
    """Only genuine Tally tags are read; the old invented flat tags are ignored."""

    def setUp(self):
        from tally_migrator.tally.extractors import LEDGER_TAGS, ITEM_TAGS
        self.source = FileTallySource(REAL_TALLY_XML)
        self.LEDGER_TAGS = LEDGER_TAGS
        self.ITEM_TAGS = ITEM_TAGS

    def _ledgers(self):
        rows = self.source.get_collection(
            "Ledger", ["Address", "LedgerEmail", "LedgerState"], self.LEDGER_TAGS)
        return {r["_name"]: r for r in rows}

    def test_invented_flat_tags_are_ignored(self):
        # <LEDGERSTATE>/<LEDGEREMAIL>/flat <ADDRESS> are no longer read.
        row = self._ledgers()["Invented Tag Co"]
        self.assertEqual(row["LedgerState"], "")
        self.assertEqual(row["LedgerEmail"], "")
        self.assertEqual(row["Address"], "")

    def test_real_tally_state_and_email(self):
        row = self._ledgers()["Real Tally Co"]
        self.assertEqual(row["LedgerState"], "Karnataka")   # <LEDSTATENAME>
        self.assertEqual(row["LedgerEmail"], "real@example.com")  # <EMAIL>

    def test_multiline_address_list_is_joined(self):
        row = self._ledgers()["Real Tally Co"]
        self.assertEqual(row["Address"], "Door No 5, MG Road, Bengaluru")

    def test_standard_price_and_cost_revision_lists(self):
        item = self.source.get_collection(
            "Stock Item", ["StandardPrice", "StandardCost"], self.ITEM_TAGS)[0]
        self.assertEqual(item["StandardPrice"], "99.50")
        self.assertEqual(item["StandardCost"], "72.00")


# A real Tally Prime export nests the ledger's bank account under PAYMENTDETAILS.LIST
# (ACCOUNTNUMBER / IFSCODE / BANKNAME), not the older flat <BANKDETAILS> tag. Before
# the nested path was mapped, every such party's bank account silently dropped.
BANK_NESTED_XML = """<ENVELOPE>
  <BODY><IMPORTDATA><REQUESTDATA>
    <TALLYMESSAGE>
      <LEDGER NAME="Cleavland Wears"><PARENT>Sundry Debtors</PARENT>
        <PAYMENTDETAILS.LIST>
          <IFSCODE>KOTK006777</IFSCODE>
          <BANKNAME>Kotak Bank</BANKNAME>
          <ACCOUNTNUMBER>723801504492</ACCOUNTNUMBER>
        </PAYMENTDETAILS.LIST></LEDGER>
    </TALLYMESSAGE>
    <TALLYMESSAGE>
      <LEDGER NAME="Flat Shape Co"><PARENT>Sundry Debtors</PARENT>
        <BANKDETAILS>999888777</BANKDETAILS>
        <IFSCODE>HDFC0000001</IFSCODE></LEDGER>
    </TALLYMESSAGE>
  </REQUESTDATA></IMPORTDATA></BODY>
</ENVELOPE>"""


class TestBankDetails(unittest.TestCase):
    """Bank account is read from the nested PAYMENTDETAILS.LIST shape AND the older
    flat tags, so either export populates the ERPNext Bank Account."""

    def setUp(self):
        from tally_migrator.tally.extractors import LEDGER_TAGS
        self.source = FileTallySource(BANK_NESTED_XML)
        self.rows = {
            r["_name"]: r for r in self.source.get_collection(
                "Ledger", ["BankAccountNo", "BankIFSC", "BankName"], LEDGER_TAGS)}

    def test_nested_payment_details_bank_account_is_read(self):
        row = self.rows["Cleavland Wears"]
        self.assertEqual(row["BankAccountNo"], "723801504492")
        self.assertEqual(row["BankIFSC"], "KOTK006777")
        self.assertEqual(row["BankName"], "Kotak Bank")

    def test_flat_bank_shape_still_read(self):
        row = self.rows["Flat Shape Co"]
        self.assertEqual(row["BankAccountNo"], "999888777")
        self.assertEqual(row["BankIFSC"], "HDFC0000001")


# A faithful slice of a genuine Tally Prime *export*: party mailing + GST details
# nested in LEDMAILINGDETAILS.LIST / LEDGSTREGDETAILS.LIST, and a Tally ``&#4;``
# illegal control-char reference (it prefixes "Not Applicable" values). Modelled on
# real files (Master New.xml / Moooor.xml).
REAL_EXPORT_XML = """<ENVELOPE>
  <BODY><IMPORTDATA><REQUESTDATA>
    <TALLYMESSAGE>
      <GROUP NAME="Sundry Debtors"><PARENT>Current Assets</PARENT></GROUP>
    </TALLYMESSAGE>
    <TALLYMESSAGE>
      <LEDGER NAME="Garachh">
        <PARENT>Sundry Debtors</PARENT>
        <GSTTYPE>&#4; Not Applicable</GSTTYPE>
        <OPENINGBALANCE>-100000.00</OPENINGBALANCE>
        <LEDGSTREGDETAILS.LIST>
          <GSTREGISTRATIONTYPE>Regular</GSTREGISTRATIONTYPE>
          <GSTIN>24AAACC1206D1ZM</GSTIN>
        </LEDGSTREGDETAILS.LIST>
        <LEDMAILINGDETAILS.LIST>
          <ADDRESS.LIST TYPE="String">
            <ADDRESS>Testing</ADDRESS>
            <ADDRESS>Addresss</ADDRESS>
          </ADDRESS.LIST>
          <PINCODE>400086</PINCODE>
          <MAILINGNAME>Garachh</MAILINGNAME>
          <STATE>Maharashtra</STATE>
          <COUNTRY>India</COUNTRY>
        </LEDMAILINGDETAILS.LIST>
      </LEDGER>
    </TALLYMESSAGE>
  </REQUESTDATA></IMPORTDATA></BODY>
</ENVELOPE>"""


class TestCacheIsolation(unittest.TestCase):
    """The source caches its parse across requests (api._SOURCE_CACHE), so a consumer
    that mutates a returned record dict must not poison the cache for later calls."""

    def test_get_collection_returns_isolated_copies(self):
        from tally_migrator.tally.extractors import LEDGER_TAGS
        src = FileTallySource(REAL_EXPORT_XML)
        first = src.get_collection("Ledger", ["LedgerState"], LEDGER_TAGS)
        first[0]["LedgerState"] = "MUTATED"          # simulate apply_record_overrides
        second = src.get_collection("Ledger", ["LedgerState"], LEDGER_TAGS)
        self.assertNotEqual(second[0]["LedgerState"], "MUTATED")

    def test_overrides_applied_twice_still_logged(self):
        """The real bug: a Re-check applies overrides once (mutating the cached
        parse), then the run applies them again on the SAME cached source. Without
        isolated copies the second apply sees old == new and the edit vanishes from
        the audit. With copies, every apply records the change."""
        from tally_migrator.tally.extractors import TallyExtractor
        from tally_migrator.migration.overrides import apply_record_overrides
        src = FileTallySource(REAL_EXPORT_XML)
        ext = TallyExtractor(src)
        overrides = {"Customer": {"Garachh": {"LedgerState": "Gujarat"}}}

        log1 = []
        apply_record_overrides(ext.extract_all(), overrides, log1)
        log2 = []
        apply_record_overrides(ext.extract_all(), overrides, log2)

        self.assertEqual(len(log1), 1)
        self.assertEqual(len(log2), 1, "cached parse was poisoned - second apply lost the edit")
        self.assertEqual(log2[0]["old"], "Maharashtra")
        self.assertEqual(log2[0]["new"], "Gujarat")


class TestDecode(unittest.TestCase):
    def test_utf16_bom_is_decoded(self):
        raw = REAL_EXPORT_XML.encode("utf-16")  # adds a UTF-16 LE BOM
        self.assertTrue(raw[:2] in (b"\xff\xfe", b"\xfe\xff"))
        text = decode_tally_bytes(raw)
        self.assertIn("<LEDGER NAME=\"Garachh\">", text)

    def test_utf8_passthrough(self):
        raw = REAL_EXPORT_XML.encode("utf-8")
        self.assertIn("Garachh", decode_tally_bytes(raw))

    def test_str_passthrough(self):
        self.assertEqual(decode_tally_bytes("already text"), "already text")

    def test_sanitize_strips_illegal_char_ref(self):
        self.assertNotIn("&#4;", sanitize_tally_xml("x &#4; y"))
        # a legal reference is preserved
        self.assertIn("&#65;", sanitize_tally_xml("&#65;"))


class TestXmlSafety(unittest.TestCase):
    """A DTD / entity declaration (the 'billion laughs' DoS vector) is refused -
    a real Tally masters export never declares one."""

    def test_doctype_is_rejected(self):
        payload = ('<?xml version="1.0"?>'
                   '<!DOCTYPE lolz [<!ENTITY lol "lol">]>'
                   '<ENVELOPE>&lol;</ENVELOPE>')
        with self.assertRaises(Exception):
            FileTallySource(payload)

    def test_entity_declaration_is_rejected(self):
        with self.assertRaises(Exception):
            FileTallySource('<!ENTITY x "y"><ENVELOPE/>')

    def test_clean_export_still_parses(self):
        # No DTD - must not be falsely rejected.
        src = FileTallySource("<ENVELOPE><TALLYMESSAGE/></ENVELOPE>")
        self.assertTrue(src.ping())

    def test_collection_result_is_cached_per_signature(self):
        """extract_all + extract_coa both request Group/Ledger; the second call
        returns the memoised parse - but as a fresh copy, never the same object, so a
        consumer that mutates the first result can't poison the second (see
        TestCacheIsolation)."""
        src = FileTallySource(SAMPLE_XML)
        first = src.get_collection("Ledger", ["Parent", "OpeningBalance"])
        second = src.get_collection("Ledger", ["Parent", "OpeningBalance"])
        self.assertEqual(first, second)        # same data (parse memoised)
        self.assertIsNot(first, second)        # but isolated copies


class TestRealExportFormat(unittest.TestCase):
    """A genuine export (UTF-16 + &#4; + nested .LIST containers) parses and the
    party's mailing/GST fields extract from their real nested paths."""

    def _ledger(self):
        raw = REAL_EXPORT_XML.encode("utf-16")
        source = FileTallySource(decode_tally_bytes(raw))
        rows = source.get_collection("Ledger", LEDGER_FIELDS, LEDGER_TAGS)
        return next(r for r in rows if r["_name"] == "Garachh")

    def test_illegal_ref_does_not_break_parsing(self):
        # If &#4; weren't stripped, FileTallySource() would raise.
        self.assertEqual(self._ledger()["_name"], "Garachh")

    def test_nested_gst_details(self):
        led = self._ledger()
        self.assertEqual(led["GSTRegistrationNumber"], "24AAACC1206D1ZM")
        self.assertEqual(led["GSTRegistrationType"], "Regular")

    def test_nested_mailing_details(self):
        led = self._ledger()
        self.assertEqual(led["LedgerState"], "Maharashtra")
        self.assertEqual(led["PinCode"], "400086")
        self.assertEqual(led["MailingName"], "Garachh")
        self.assertEqual(led["CountryName"], "India")
        self.assertEqual(led["Address"], "Testing, Addresss")

    def test_opening_balance_top_level(self):
        self.assertEqual(self._ledger()["OpeningBalance"], "-100000.00")


# A faithful slice of a genuine Stock Item export: the HSN code nests in
# HSNDETAILS.LIST/HSNCODE while the sibling <HSN> holds the *description*;
# taxability + supply type nest under GSTDETAILS.LIST. Modelled on a real
# TallyPrime collection dump (full_MyStockItems2.xml).
REAL_ITEM_XML = """<ENVELOPE>
  <BODY><DATA><COLLECTION>
    <STOCKITEM NAME="Wireless Mouse - Logitech M185">
      <PARENT>Computer Accessories</PARENT>
      <BASEUNITS>Nos</BASEUNITS>
      <ADDITIONALUNITS>&#4; Not Applicable</ADDITIONALUNITS>
      <HSNDETAILS.LIST>
        <APPLICABLEFROM>20260401</APPLICABLEFROM>
        <HSNCODE>847160</HSNCODE>
        <HSN>Computer input devices</HSN>
      </HSNDETAILS.LIST>
      <GSTDETAILS.LIST>
        <SUPPLYTYPE>Goods</SUPPLYTYPE>
        <TAXABILITY>Taxable</TAXABILITY>
        <SRCOFGSTDETAILS>As per Company/Stock Group</SRCOFGSTDETAILS>
      </GSTDETAILS.LIST>
    </STOCKITEM>
  </COLLECTION></DATA></BODY>
</ENVELOPE>"""


class TestRealItemSchema(unittest.TestCase):
    """The nested HSN/GST tags confirmed against a real Stock Item export."""

    def _item(self):
        source = FileTallySource(REAL_ITEM_XML)
        return source.get_collection("Stock Item", ITEM_FIELDS, ITEM_TAGS)[0]

    def test_hsn_code_from_nested_hsncode_not_description(self):
        # The value must be the code (847160), never the sibling <HSN> description.
        self.assertEqual(self._item()["HSNCode"], "847160")

    def test_gst_taxability_and_supply_type(self):
        item = self._item()
        self.assertEqual(item["GSTTaxability"], "Taxable")
        self.assertEqual(item["TypeOfSupply"], "Goods")

    def test_base_unit_is_plain_string_reference(self):
        self.assertEqual(self._item()["BaseUnits"], "Nos")


class TestUtf16Decoding(unittest.TestCase):
    """Real TallyPrime exports are UTF-16-with-BOM. Regression guard for the upload
    failure where the BOM reached the XML parser and it died at line 1, column 0."""

    def test_utf16_le_bom_decodes_and_parses(self):
        raw = SAMPLE_XML.encode("utf-16")  # adds the LE BOM
        self.assertEqual(raw[:2], b"\xff\xfe")
        src = FileTallySource(decode_tally_bytes(raw))
        self.assertEqual(src.get_collection("Godown", ["Name"])[0]["_name"], "Main Store")

    def test_utf16_be_bom_decodes(self):
        raw = b"\xfe\xff" + SAMPLE_XML.encode("utf-16-be")  # prepend BE BOM
        self.assertIn("<ENVELOPE>", sanitize_tally_xml(decode_tally_bytes(raw)))

    def test_bom_stripped_so_parser_sees_clean_root(self):
        # A leading BOM character must not survive into the text handed to ElementTree.
        text = "﻿" + SAMPLE_XML
        self.assertTrue(sanitize_tally_xml(text).startswith("<ENVELOPE>"))

    def test_str_passthrough_is_unchanged(self):
        # decode_tally_bytes must not mangle an already-decoded str; the byte-level
        # recovery now happens in api._raw_file_bytes (reads binary before decoding).
        self.assertEqual(decode_tally_bytes(SAMPLE_XML), SAMPLE_XML)


# A faithful slice of a real export's bill-wise opening detail: one debtor with
# three outstanding bills that sum to its ledger opening (signs as Tally emits
# them - negative = Dr), one ledger with an empty allocation list, and one
# ledger carrying an advance flag. Mirrors what MAX.xml actually contains.
BILLWISE_XML = """<ENVELOPE><BODY><IMPORTDATA><REQUESTDATA>
  <TALLYMESSAGE>
    <LEDGER NAME="ABC Company Limited"><PARENT>Sundry Debtors</PARENT>
      <OPENINGBALANCE>-30000.00</OPENINGBALANCE>
      <BILLALLOCATIONS.LIST>
        <BILLDATE>20200310</BILLDATE><NAME>ABC/1</NAME>
        <ISADVANCE>No</ISADVANCE><OPENINGBALANCE>-3000.00</OPENINGBALANCE>
      </BILLALLOCATIONS.LIST>
      <BILLALLOCATIONS.LIST>
        <BILLDATE>20200312</BILLDATE><NAME>ABC/2</NAME>
        <ISADVANCE>No</ISADVANCE><OPENINGBALANCE>-6000.00</OPENINGBALANCE>
      </BILLALLOCATIONS.LIST>
      <BILLALLOCATIONS.LIST>
        <BILLDATE>20200314</BILLDATE><NAME>ABC/3</NAME>
        <ISADVANCE>No</ISADVANCE><OPENINGBALANCE>-21000.00</OPENINGBALANCE>
      </BILLALLOCATIONS.LIST>
    </LEDGER>
  </TALLYMESSAGE>
  <TALLYMESSAGE>
    <LEDGER NAME="No Bills Co"><PARENT>Sundry Debtors</PARENT>
      <OPENINGBALANCE>-5000.00</OPENINGBALANCE>
      <BILLALLOCATIONS.LIST>      </BILLALLOCATIONS.LIST>
    </LEDGER>
  </TALLYMESSAGE>
  <TALLYMESSAGE>
    <LEDGER NAME="Advance Holder"><PARENT>Sundry Debtors</PARENT>
      <OPENINGBALANCE>2000.00</OPENINGBALANCE>
      <BILLALLOCATIONS.LIST>
        <BILLDATE>20200401</BILLDATE><NAME>ADV-1</NAME>
        <ISADVANCE>Yes</ISADVANCE><OPENINGBALANCE>2000.00</OPENINGBALANCE>
      </BILLALLOCATIONS.LIST>
    </LEDGER>
  </TALLYMESSAGE>
</REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>"""


class TestGetChildList(unittest.TestCase):
    """The repeating-child reader returns one row per BILLALLOCATIONS.LIST."""

    def setUp(self):
        self.source = FileTallySource(BILLWISE_XML)

    def test_returns_one_row_per_bill_with_parent(self):
        rows = self.source.get_child_list(
            "Ledger", "BILLALLOCATIONS.LIST",
            ["BillDate", "Name", "IsAdvance", "OpeningBalance"])
        abc = [r for r in rows if r["_parent"] == "ABC Company Limited"]
        self.assertEqual(len(abc), 3)
        self.assertEqual(abc[0]["_name"], "ABC/1")          # NAME also under _name
        self.assertEqual(abc[0]["BillDate"], "20200310")
        self.assertEqual(abc[0]["OpeningBalance"], "-3000.00")

    def test_empty_list_row_has_blank_fields(self):
        # An empty <BILLALLOCATIONS.LIST> is still a child element, so the raw
        # reader returns a blank-field row for it; dropping zero-amount bills is
        # the extractor's job, not this reader's (kept faithful + unopinionated).
        rows = self.source.get_child_list(
            "Ledger", "BILLALLOCATIONS.LIST", ["Name", "OpeningBalance"])
        empty = [r for r in rows if r["_parent"] == "No Bills Co"]
        self.assertEqual(len(empty), 1)
        self.assertEqual(empty[0]["Name"], "")
        self.assertEqual(empty[0]["OpeningBalance"], "")

    def test_cached_call_is_stable(self):
        a = self.source.get_child_list("Ledger", "BILLALLOCATIONS.LIST", ["Name"])
        b = self.source.get_child_list("Ledger", "BILLALLOCATIONS.LIST", ["Name"])
        self.assertEqual(a, b)        # memoised parse, same data
        self.assertIsNot(a, b)        # but isolated copies (no cross-call mutation)


class TestExtractBillAllocations(unittest.TestCase):
    """End-to-end parse: XML → list[BillAllocation] with correct signs/dates."""

    def setUp(self):
        self.bills = TallyExtractor(
            FileTallySource(BILLWISE_XML)).extract_bill_allocations()

    def _by_party(self, party):
        return [b for b in self.bills if b.party == party]

    def test_outstanding_bills_parsed_with_dr_sign(self):
        abc = self._by_party("ABC Company Limited")
        self.assertEqual(len(abc), 3)
        self.assertTrue(all(b.dr_cr == "Dr" for b in abc))   # negative → Dr
        self.assertFalse(any(b.is_advance for b in abc))
        self.assertEqual(round(sum(b.amount for b in abc), 2), 30000.0)

    def test_bill_date_is_iso(self):
        first = self._by_party("ABC Company Limited")[0]
        self.assertEqual(first.bill_date, "2020-03-10")
        self.assertEqual(first.bill_no, "ABC/1")

    def test_advance_flag_and_cr_sign(self):
        adv = self._by_party("Advance Holder")
        self.assertEqual(len(adv), 1)
        self.assertTrue(adv[0].is_advance)
        self.assertEqual(adv[0].dr_cr, "Cr")                 # positive → Cr

    def test_party_without_bills_absent(self):
        self.assertEqual(self._by_party("No Bills Co"), [])

    def test_source_without_child_support_degrades(self):
        class _Flat:
            def get_collection(self, *a, **k):
                return []
        self.assertEqual(
            TallyExtractor(_Flat()).extract_bill_allocations(), [])


if __name__ == "__main__":
    unittest.main()
