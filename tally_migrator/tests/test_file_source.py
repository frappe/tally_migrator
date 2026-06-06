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


if __name__ == "__main__":
    unittest.main()
