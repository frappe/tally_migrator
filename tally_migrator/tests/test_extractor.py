"""Unit tests for TallyExtractor (no Tally connection required - uses mock data)."""
import unittest

from tally_migrator.tally.extractors import TallyExtractor


class _MockClient:
    """Returns deterministic fixture data without hitting Tally."""

    def get_collection(self, obj_type, fields, tag_map=None):
        if obj_type == "Group":
            return [
                {"_name": "Primary", "Parent": ""},
                {"_name": "Sundry Debtors", "Parent": "Primary"},
                {"_name": "Retail Debtors", "Parent": "Sundry Debtors"},
                {"_name": "Sundry Creditors", "Parent": "Primary"},
                {"_name": "Capital Account", "Parent": "Primary"},
            ]
        if obj_type == "Ledger":
            return [
                {"_name": "Customer A", "Parent": "Sundry Debtors"},
                {"_name": "Customer B", "Parent": "Retail Debtors"},
                {"_name": "Supplier X", "Parent": "Sundry Creditors"},
                {"_name": "Capital Ledger", "Parent": "Capital Account"},
            ]
        if obj_type == "Stock Item":
            return [{"_name": "Widget", "Parent": "All Items", "BaseUnits": "Nos"}]
        if obj_type == "Godown":
            return [
                {"_name": "Main Store", "Parent": ""},
                {"_name": "Shelf A", "Parent": "Main Store"},
            ]
        if obj_type == "Stock Group":
            return [
                {"_name": "Electronics", "Parent": ""},
                {"_name": "Phones", "Parent": "Electronics"},
            ]
        if obj_type == "Unit":
            return [
                {"_name": "Nos", "IsSimpleUnit": "Yes", "DecimalPlaces": "0"},
                {"_name": "Doz of 12 Nos", "IsSimpleUnit": "No",
                 "BaseUnits": "Doz", "AdditionalUnits": "Nos", "Conversion": "12"},
            ]
        return []

    def get_child_list(self, obj_type, child_tag, fields):
        if obj_type == "Ledger" and child_tag == "LEDMULTIADDRESSLIST.LIST":
            return [{"_parent": "Customer A",
                     "ADDRESS.LIST/ADDRESS": "Godown 5, Karnataka",
                     "ADDRESSNAME": "Warehouse",
                     "PINCODE": "560001"}]
        if obj_type == "Ledger" and child_tag == "CONTACTDETAILS.LIST":
            return [{"_parent": "Customer A", "Name": "Accounts",
                     "PhoneNumber": "9496278969", "IsDefaultWhatsAppNum": "Yes"}]
        return []


class TestTallyExtractor(unittest.TestCase):
    def setUp(self):
        self.extractor = TallyExtractor(_MockClient())

    def test_customers_include_direct_and_nested(self):
        masters = self.extractor.extract_all()
        names = [c["_name"] for c in masters.customers]
        self.assertIn("Customer A", names)   # direct child of Sundry Debtors
        self.assertIn("Customer B", names)   # child of Retail Debtors (nested)

    def test_suppliers_correctly_classified(self):
        masters = self.extractor.extract_all()
        names = [s["_name"] for s in masters.suppliers]
        self.assertIn("Supplier X", names)

    def test_non_debtor_ledger_excluded_from_customers(self):
        masters = self.extractor.extract_all()
        names = [c["_name"] for c in masters.customers]
        self.assertNotIn("Capital Ledger", names)

    def test_items_and_warehouses_fetched(self):
        masters = self.extractor.extract_all()
        self.assertEqual(len(masters.items), 1)
        self.assertEqual(len(masters.warehouses), 2)

    def test_stock_groups_and_units_fetched(self):
        masters = self.extractor.extract_all()
        self.assertEqual({g["_name"] for g in masters.stock_groups},
                         {"Electronics", "Phones"})
        self.assertEqual(len(masters.units), 2)
        self.assertEqual(masters.summary["stock_groups"], 2)
        self.assertEqual(masters.summary["units"], 2)

    def test_summary_counts_correct(self):
        masters = self.extractor.extract_all()
        s = masters.summary
        self.assertEqual(s["customers"], 2)
        self.assertEqual(s["suppliers"], 1)
        self.assertEqual(s["items"], 1)
        self.assertEqual(s["warehouses"], 2)

    def test_party_address_book_and_contacts_attached(self):
        """A party's repeating address-book + extra phone contacts are attached to
        its ledger record (_extra_addresses / _extra_contacts); parties without such
        child rows get no extra keys."""
        masters = self.extractor.extract_all()
        a = next(c for c in masters.customers if c["_name"] == "Customer A")
        self.assertEqual(a["_extra_addresses"],
                         [{"address": "Godown 5, Karnataka", "name": "Warehouse",
                           "state": "", "pincode": "560001"}])
        self.assertEqual(len(a["_extra_contacts"]), 1)
        self.assertEqual(a["_extra_contacts"][0]["phone"], "9496278969")
        self.assertTrue(a["_extra_contacts"][0]["whatsapp"])
        b = next(c for c in masters.customers if c["_name"] == "Customer B")
        self.assertNotIn("_extra_addresses", b)
        self.assertNotIn("_extra_contacts", b)

    def test_parse_quantity_handles_unit_suffix(self):
        """Stock opening quantities are unit-suffixed ('55 Nos') in real Tally
        exports; the amount parser reads them as 0, so the quantity parser must
        recover the number. Regression for the 'opening stock imports as zero' bug."""
        pq = TallyExtractor._parse_quantity
        self.assertEqual(pq(" 55 Nos"), 55.0)
        self.assertEqual(pq("100.50 Kgs"), 100.5)
        self.assertEqual(pq("1,200 Nos"), 1200.0)
        self.assertEqual(pq("-5 Nos"), -5.0)
        self.assertEqual(pq("10.00 Nos = 800.00"), 800.0)
        self.assertEqual(pq(""), 0.0)
        self.assertEqual(pq(None), 0.0)
        self.assertEqual(pq("Nos"), 0.0)
        # The amount parser genuinely cannot read these - confirms why we needed a
        # separate quantity parser.
        self.assertEqual(TallyExtractor._parse_opening(" 55 Nos")[0], 0.0)

    def test_parse_rate_handles_unit_suffix(self):
        """Opening rates export unit-suffixed ('1.00/Nos'); _to_float reads them as
        0, losing valuation. The rate parser must recover the number (and drop the
        Tally sign - direction lives on qty/value, not the rate)."""
        pr = TallyExtractor._parse_rate
        self.assertEqual(pr("1.00/Nos"), 1.0)
        self.assertEqual(pr("12.50/Kg"), 12.5)
        self.assertEqual(pr("1,250.00/Box"), 1250.0)
        self.assertEqual(pr("-1.00/Nos"), 1.0)   # magnitude only
        self.assertEqual(pr("75.00"), 75.0)
        self.assertEqual(pr(""), 0.0)
        self.assertEqual(pr(None), 0.0)
        self.assertEqual(pr("/Nos"), 0.0)

    def test_bfs_handles_deep_nesting(self):
        """Groups 3 levels deep must still be recognised as Debtors."""

        class DeepClient(_MockClient):
            def get_collection(self, obj_type, fields, tag_map=None):
                if obj_type == "Group":
                    return [
                        {"_name": "Sundry Debtors", "Parent": "Primary"},
                        {"_name": "Level 2", "Parent": "Sundry Debtors"},
                        {"_name": "Level 3", "Parent": "Level 2"},
                    ]
                if obj_type == "Ledger":
                    return [{"_name": "Deep Customer", "Parent": "Level 3"}]
                return []

        ext = TallyExtractor(DeepClient())
        masters = ext.extract_all()
        self.assertEqual(len(masters.customers), 1)
