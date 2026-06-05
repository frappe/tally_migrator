"""Unit tests for TallyExtractor (no Tally connection required — uses mock data)."""
import unittest

from tally_migrator.tally.extractors import TallyExtractor


class _MockClient:
    """Returns deterministic fixture data without hitting Tally."""

    def get_collection(self, obj_type, fields):
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

    def test_summary_counts_correct(self):
        masters = self.extractor.extract_all()
        s = masters.summary
        self.assertEqual(s["customers"], 2)
        self.assertEqual(s["suppliers"], 1)
        self.assertEqual(s["items"], 1)
        self.assertEqual(s["warehouses"], 2)

    def test_bfs_handles_deep_nesting(self):
        """Groups 3 levels deep must still be recognised as Debtors."""

        class DeepClient(_MockClient):
            def get_collection(self, obj_type, fields):
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
