"""Unit tests for FileTallySource (offline Tally masters XML parsing).

No Tally connection and no Frappe site required — exercises the parser and its
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
# <ADDRESS>/<STANDARDPRICE>) which the parser must now IGNORE — only real Tally
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
    """A DTD / entity declaration (the 'billion laughs' DoS vector) is refused —
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
        # No DTD — must not be falsely rejected.
        src = FileTallySource("<ENVELOPE><TALLYMESSAGE/></ENVELOPE>")
        self.assertTrue(src.ping())

    def test_collection_result_is_cached_per_signature(self):
        """extract_all + extract_coa both request Group/Ledger; the second call
        must return the memoised result, not re-walk the DOM."""
        src = FileTallySource(SAMPLE_XML)
        first = src.get_collection("Ledger", ["Parent", "OpeningBalance"])
        second = src.get_collection("Ledger", ["Parent", "OpeningBalance"])
        self.assertIs(first, second)


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


if __name__ == "__main__":
    unittest.main()
