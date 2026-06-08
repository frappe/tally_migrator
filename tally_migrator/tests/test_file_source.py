"""Unit tests for FileTallySource (offline Tally masters XML parsing).

No Tally connection and no Frappe site required — exercises the parser and its
interop with TallyExtractor directly.
"""
import unittest

from tally_migrator.tally.file_source import FileTallySource
from tally_migrator.tally.extractors import TallyExtractor


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


if __name__ == "__main__":
    unittest.main()
