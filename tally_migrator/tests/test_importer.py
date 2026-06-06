"""
Integration tests for the ERPNext importers.

These hit a real Frappe/ERPNext database — run via ``bench run-tests``. Records
are cleaned up explicitly (the importer commits per record, so rollback alone is
insufficient). Warehouse import requires a configured Company and skips without one.
"""
import unittest

import frappe

from tally_migrator.tests.utils import get_company, require_company, cleanup_test_records


class TestERPNextImporter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        frappe.set_user("Administrator")
        cleanup_test_records()  # ensure a clean slate for repeatable runs
        cls.company = get_company()
        from tally_migrator.erpnext.importers import ERPNextImporter

        cls.importer = ERPNextImporter(cls.company or "")

    @classmethod
    def tearDownClass(cls):
        cleanup_test_records()

    # ── Customer ──────────────────────────────────────────────────────────────

    def test_import_customer_creates_record(self):
        customer = {
            "_name": "_TMTest Customer",
            "GSTRegistrationNumber": "27AAACT2727Q1ZW",
            "INCOMETAXNumber": "AAACT2727Q",
            "BillCreditPeriod": "30",
        }
        result = self.importer.import_customers([customer])
        self.assertEqual(result.failed, 0, msg=str(result.errors))
        self.assertEqual(result.created, 1)
        self.assertTrue(frappe.db.exists("Customer", {"customer_name": "_TMTest Customer"}))

    def test_import_customer_skips_duplicate(self):
        customer = {"_name": "_TMTest Customer Dup", "GSTRegistrationNumber": ""}
        self.importer.import_customers([customer])
        result = self.importer.import_customers([customer])
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.created, 0)

    def test_reimport_does_not_duplicate_address(self):
        """Re-running must skip the existing party and NOT add a second address."""
        customer = {
            "_name": "_TMTest Customer Addr",
            "Address": "12 Test Street",
            "LedgerState": "Maharashtra",
            "PinCode": "400001",
        }
        self.importer.import_customers([customer])
        self.importer.import_customers([customer])  # re-run
        addresses = frappe.get_all(
            "Address", filters={"address_title": "_TMTest Customer Addr"}
        )
        self.assertEqual(len(addresses), 1, msg="re-run duplicated the address")

    # ── Supplier ──────────────────────────────────────────────────────────────

    def test_import_supplier_creates_record(self):
        supplier = {"_name": "_TMTest Supplier", "GSTRegistrationNumber": "", "INCOMETAXNumber": ""}
        result = self.importer.import_suppliers([supplier])
        self.assertEqual(result.failed, 0, msg=str(result.errors))
        self.assertEqual(result.created, 1)

    # ── Item ──────────────────────────────────────────────────────────────────

    def test_import_item_creates_record(self):
        item = {
            "_name": "_TMTest Item",
            "Parent": "All Item Groups",
            "BaseUnits": "Nos",
            "StandardPrice": "100",
            "StandardCost": "80",
            "HSNCode": "99041010",
        }
        result = self.importer.import_items([item])
        self.assertEqual(result.failed, 0, msg=str(result.errors))
        self.assertEqual(result.created, 1)

    def test_item_uom_mapped_correctly(self):
        item = {"_name": "_TMTest Item KG", "Parent": "All Item Groups", "BaseUnits": "Kgs"}
        self.importer.import_items([item])
        uom = frappe.db.get_value("Item", {"item_name": "_TMTest Item KG"}, "stock_uom")
        self.assertEqual(uom, "Kg")

    # ── Warehouse (requires a configured Company) ───────────────────────────────

    def test_warehouse_topo_sort_creates_parent_first(self):
        require_company()
        warehouses = [
            {"_name": "_TMTest Child WH", "Parent": "_TMTest Parent WH", "Address": ""},
            {"_name": "_TMTest Parent WH", "Parent": "", "Address": ""},
        ]
        result = self.importer.import_warehouses(warehouses)
        self.assertEqual(result.failed, 0, msg=str(result.errors))
        self.assertEqual(result.created, 2)

    # ── ImportResult helper ─────────────────────────────────────────────────────

    def test_import_result_as_dict_structure(self):
        from tally_migrator.erpnext.importers import ImportResult

        r = ImportResult("Customer")
        r.created = 5
        r.skipped = 2
        r.add_error("Bad Name", "Invalid GST")
        d = r.as_dict()
        self.assertEqual(d["created"], 5)
        self.assertEqual(d["skipped"], 2)
        self.assertEqual(d["failed"], 1)
        self.assertEqual(len(d["errors"]), 1)
