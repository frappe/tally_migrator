"""
Integration tests for the ERPNext importers.

These hit a real Frappe/ERPNext database - run via ``bench run-tests``. Records
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

    def test_contact_created_with_phone_and_email(self):
        customer = {
            "_name": "_TMTest Customer Contact",
            "LedgerMobile": "9876543210",
            "LedgerEmail": "buyer@example.com",
        }
        self.importer.import_customers([customer])
        contacts = frappe.get_all(
            "Contact", filters={"first_name": "_TMTest Customer Contact"}, pluck="name")
        self.assertEqual(len(contacts), 1)
        doc = frappe.get_doc("Contact", contacts[0])
        self.assertIn("buyer@example.com", [e.email_id for e in doc.email_ids])
        self.assertIn("9876543210", [p.phone for p in doc.phone_nos])

    def test_no_contact_when_no_phone_or_email(self):
        customer = {"_name": "_TMTest Customer NoContact"}
        self.importer.import_customers([customer])
        self.assertFalse(
            frappe.db.exists("Contact", {"first_name": "_TMTest Customer NoContact"}))

    def test_reimport_does_not_duplicate_contact(self):
        customer = {"_name": "_TMTest Customer ContactDup", "LedgerEmail": "x@example.com"}
        self.importer.import_customers([customer])
        self.importer.import_customers([customer])  # re-run
        contacts = frappe.get_all(
            "Contact", filters={"first_name": "_TMTest Customer ContactDup"})
        self.assertEqual(len(contacts), 1, msg="re-run duplicated the contact")

    def test_explicit_gst_registration_type_wins(self):
        """Tally's stated registration type overrides GSTIN/country inference."""
        if not frappe.db.has_column("Customer", "gst_category"):
            self.skipTest("gst_category requires the India Compliance app")
        customer = {
            "_name": "_TMTest Customer Comp",
            "GSTRegistrationNumber": "27AAACT2727Q1ZW",   # would infer Registered Regular
            "GSTRegistrationType": "Composition",
        }
        self.importer.import_customers([customer])
        cat = frappe.db.get_value(
            "Customer", {"customer_name": "_TMTest Customer Comp"}, "gst_category")
        self.assertEqual(cat, "Registered Composition")

    def test_party_gstin_field_set_so_india_compliance_keeps_category(self):
        """Regression: India Compliance owns the party `gstin` field and recomputes
        gst_category from it on validate. If the importer sets only `tax_id`, IC
        clobbers the category to 'Unregistered'. The importer must also set `gstin`
        (when valid + the field exists) so IC preserves the computed category."""
        if not frappe.db.has_column("Customer", "gstin"):
            self.skipTest("gstin field requires the India Compliance app")
        customer = {
            "_name": "_TMTest Customer GSTIN",
            "GSTRegistrationNumber": "27AAACT2727Q1ZW",
            "GSTRegistrationType": "Regular",
            "LedgerState": "Maharashtra",
        }
        self.importer.import_customers([customer])
        row = frappe.db.get_value(
            "Customer", {"customer_name": "_TMTest Customer GSTIN"},
            ["gstin", "gst_category"], as_dict=True)
        self.assertEqual(row.gstin, "27AAACT2727Q1ZW")
        self.assertEqual(row.gst_category, "Registered Regular")

    def test_credit_limit_imported(self):
        customer = {"_name": "_TMTest Customer Credit", "CreditLimit": "200000"}
        self.importer.import_customers([customer])
        doc = frappe.get_doc("Customer", {"customer_name": "_TMTest Customer Credit"})
        self.assertTrue(doc.credit_limits)
        self.assertEqual(doc.credit_limits[0].credit_limit, 200000.0)

    def test_email_cc_added_as_second_contact_email(self):
        customer = {
            "_name": "_TMTest Customer CC",
            "LedgerEmail": "primary@example.com",
            "EmailCC": "cc@example.com",
        }
        self.importer.import_customers([customer])
        doc = frappe.get_doc("Contact", {"first_name": "_TMTest Customer CC"})
        emails = [e.email_id for e in doc.email_ids]
        self.assertIn("primary@example.com", emails)
        self.assertIn("cc@example.com", emails)

    def test_contact_person_name_used_when_present(self):
        customer = {
            "_name": "_TMTest Customer Person",
            "LedgerContact": "_TMTest Jane Doe",
            "LedgerEmail": "jane@example.com",
        }
        self.importer.import_customers([customer])
        # Contact is named after the contact person, not the ledger.
        self.assertTrue(frappe.db.exists("Contact", {"first_name": "_TMTest Jane Doe"}))

    def test_mailing_name_used_as_address_title(self):
        customer = {
            "_name": "_TMTest Customer Mail",
            "MailingName": "_TMTest Mailing Title",
            "Address": "5 Mailing Road",
            # India Compliance makes state mandatory on an Indian address; supply one
            # so this test exercises its actual intent (MailingName -> address_title)
            # rather than tripping IC's unrelated state requirement.
            "LedgerState": "Maharashtra",
        }
        self.importer.import_customers([customer])
        self.assertTrue(
            frappe.db.exists("Address", {"address_title": "_TMTest Mailing Title-Billing"})
            or frappe.db.exists("Address", {"address_title": "_TMTest Mailing Title"}))

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
        # The parent godown must become a GROUP warehouse, or ERPNext won't render
        # the child nested under it (the tree only expands is_group nodes).
        abbr = frappe.get_value("Company", self.company, "abbr")
        self.assertEqual(
            frappe.db.get_value("Warehouse", f"_TMTest Parent WH - {abbr}", "is_group"), 1)
        self.assertEqual(
            frappe.db.get_value("Warehouse", f"_TMTest Child WH - {abbr}", "is_group"), 0)

    # ── Stock Groups → nested Item Groups ───────────────────────────────────────

    def test_stock_groups_create_nested_item_groups(self):
        groups = [
            {"_name": "_TMTest Phones", "Parent": "_TMTest Electronics"},
            {"_name": "_TMTest Electronics", "Parent": ""},  # top-level
        ]
        result = self.importer.import_stock_groups(groups)
        self.assertEqual(result.failed, 0, msg=str(result.errors))
        self.assertEqual(result.created, 2)
        # Child nests under its Tally parent; parent nests under the default root.
        self.assertEqual(
            frappe.db.get_value("Item Group", "_TMTest Phones", "parent_item_group"),
            "_TMTest Electronics")

    # ── Units → UOM (+ conversion factor) ───────────────────────────────────────

    def test_simple_unit_creates_whole_number_uom(self):
        units = [{"_name": "_TMTest Box", "IsSimpleUnit": "Yes", "DecimalPlaces": "0"}]
        result = self.importer.import_units(units)
        self.assertEqual(result.failed, 0, msg=str(result.errors))
        self.assertTrue(frappe.db.exists("UOM", "_TMTest Box"))
        self.assertEqual(frappe.db.get_value("UOM", "_TMTest Box", "must_be_whole_number"), 1)

    def test_compound_unit_does_not_hard_fail(self):
        """Conversion-factor creation is best-effort: a failure is a warning, the
        run never errors out."""
        units = [
            {"_name": "_TMTest Doz", "IsSimpleUnit": "Yes", "DecimalPlaces": "0"},
            {"_name": "_TMTest Pcs", "IsSimpleUnit": "Yes", "DecimalPlaces": "0"},
            {"_name": "_TMTest Doz of 12", "IsSimpleUnit": "No",
             "BaseUnits": "_TMTest Doz", "AdditionalUnits": "_TMTest Pcs", "Conversion": "12"},
        ]
        result = self.importer.import_units(units)
        self.assertEqual(result.failed, 0, msg=str(result.errors))
        self.assertTrue(frappe.db.exists("UOM", "_TMTest Doz"))

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

    # ── Opening-balance plug warning (no DB) ────────────────────────────────────

    def test_opening_balance_plug_warns_when_unbalanced(self):
        from tally_migrator.erpnext.importers import OpeningBalanceImporter, ImportResult

        imp = OpeningBalanceImporter("_TMTest Co", "TC")
        result = ImportResult("Journal Entry")
        lines = [{"debit_in_account_currency": 5000.0, "credit_in_account_currency": 0.0}]
        imp._balance(lines, result)
        # A balancing Temporary Opening line is appended …
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[1]["credit_in_account_currency"], 5000.0)
        # … and the gap is surfaced as a warning, not hidden.
        self.assertEqual(result.warned, 1)
        self.assertIn("Temporary Opening", result.warnings[0]["reason"])

    def test_opening_balance_no_warn_when_balanced(self):
        from tally_migrator.erpnext.importers import OpeningBalanceImporter, ImportResult

        imp = OpeningBalanceImporter("_TMTest Co", "TC")
        result = ImportResult("Journal Entry")
        lines = [
            {"debit_in_account_currency": 5000.0, "credit_in_account_currency": 0.0},
            {"debit_in_account_currency": 0.0, "credit_in_account_currency": 5000.0},
        ]
        imp._balance(lines, result)
        self.assertEqual(len(lines), 2)          # no plug line added
        self.assertEqual(result.warned, 0)

    # ── Item-code collision (L-B) ───────────────────────────────────────────────

    def test_item_code_collision_detected(self):
        """Two distinct Tally names that reduce to the same item_code are reported,
        with the first kept and the rest listed as colliding."""
        from tally_migrator.erpnext.importers import ItemImporter

        recs = [{"_name": "A/B"}, {"_name": "A-B"}, {"_name": "Unique"}]
        collisions = ItemImporter._code_collisions(recs)
        self.assertEqual(collisions, {"A-B": ["A/B", "A-B"]})

    def test_item_code_collision_on_truncation(self):
        from tally_migrator.erpnext.importers import ItemImporter

        long_a, long_b = "x" * 200, "x" * 200 + "DIFFERENT"
        collisions = ItemImporter._code_collisions([{"_name": long_a}, {"_name": long_b}])
        self.assertEqual(len(collisions), 1)
        self.assertEqual(len(next(iter(collisions.values()))), 2)

    # ── GST category by country (M-A) ───────────────────────────────────────────

    def test_gst_category_overseas_when_country_not_india(self):
        """A party whose ledger country isn't India is 'Overseas' - its GSTIN field
        is never inspected and India is never assumed."""
        from tally_migrator.erpnext.importers import CustomerImporter

        imp = CustomerImporter("_TMTest Co", "TC")
        cat = imp._gst_category({"CountryName": "United States",
                                 "GSTRegistrationNumber": "JUNK"})
        self.assertEqual(cat, "Overseas")

    def test_gst_category_india_inspects_gstin(self):
        from tally_migrator.erpnext.importers import CustomerImporter

        imp = CustomerImporter("_TMTest Co", "TC")
        cat = imp._gst_category({"CountryName": "India", "GSTRegistrationNumber": ""})
        self.assertEqual(cat, "Unregistered")

    def test_blank_country_falls_back_to_company_not_india(self):
        """A blank Tally country uses the *company's* country, so a non-Indian
        company's parties aren't silently labelled Indian."""
        from unittest import mock
        from tally_migrator.erpnext.importers import CustomerImporter

        imp = CustomerImporter("_TMTest Co", "TC")
        with mock.patch("frappe.get_cached_value", return_value="United States"):
            cat = imp._gst_category({"CountryName": "", "GSTRegistrationNumber": ""})
        self.assertEqual(cat, "Overseas")

    # ── Re-run idempotency guards (no DB - guard stubbed) ───────────────────────

    def test_opening_balance_skipped_when_entry_exists(self):
        """A second run must NOT post a second Opening Entry (would double the books).
        Regression for the re-run double-posting bug."""
        from tally_migrator.erpnext.importers import OpeningBalanceImporter

        imp = OpeningBalanceImporter("_TMTest Co", "TC")
        # Simulate a prior, hand-built Opening Entry (a remark this migrator never
        # wrote) → conservative full skip, no double-posting.
        imp._existing_opening_state = lambda: (True, set())
        result = imp.run(accounts=[], customers=[], suppliers=[], posting_date="2024-04-01")
        self.assertEqual(result.created, 0)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.warned, 1)
        self.assertIn("not created by", result.warnings[0]["reason"])

    def test_purchase_opening_due_date_clamped_to_bill_date(self):
        """A Purchase Invoice's due date can't precede its supplier-invoice (bill)
        date - ERPNext rejects it. When a Tally bill is dated after the opening
        posting date, the due date must clamp up to the bill date, not stay at
        posting_date. Regression for the 'Due Date cannot be before Supplier
        Invoice Date' failure on real data."""
        from tally_migrator.erpnext.importers import PartyOpeningImporter

        imp = PartyOpeningImporter("_TMTest Co", "TC", "2026-04-01")
        imp._stock_uom = lambda: "Nos"
        imp._temp_account = lambda: "Temporary Opening - TC"
        imp._cost_center = lambda: "Main - TC"

        # bill dated AFTER the opening posting_date -> due_date must move up to it
        late = imp._invoice_dict("Supplier", "Pioneer Hardware", 100.0,
                                 "P/023", "2026-04-15", "marker")
        self.assertEqual(late["bill_date"], "2026-04-15")
        self.assertEqual(late["due_date"], "2026-04-15")

        # bill dated BEFORE the posting date -> due_date stays at posting_date
        early = imp._invoice_dict("Supplier", "Pioneer Hardware", 100.0,
                                  "P/099", "2026-03-10", "marker")
        self.assertEqual(early["due_date"], "2026-04-01")

    def test_extra_addresses_and_contacts_created(self):
        """A party's address book (LEDMULTIADDRESSLIST) becomes extra ERPNext Address
        rows and its additional named phones (CONTACTDETAILS) become extra Contact
        rows - beyond the single primary address/contact. Known ADDRESSNAME labels
        map to the matching address_type; others fall back to 'Other'."""
        from unittest import mock
        from tally_migrator.erpnext.importers import CustomerImporter, ImportResult

        class FakeDoc:
            def __init__(self, dt):
                self.doctype = dt
                self.tables = {}
            def append(self, tbl, row):
                self.tables.setdefault(tbl, []).append(row)
            def insert(self, **k):
                pass

        created = []
        imp = CustomerImporter("_TMTest Co", "TC")
        result = ImportResult("Customer")
        record = {
            "_name": "ACME", "CountryName": "India",
            "_extra_addresses": [
                {"address": "Godown 5, Karnataka", "name": "Warehouse"},
                {"address": "Shop 2, MG Road", "name": "Branch Office"},  # unmatched -> Other
            ],
            "_extra_contacts": [
                {"name": "Accounts", "phone": "9496278969", "whatsapp": True},
                {"name": "Dispatch", "phone": "9324652431", "whatsapp": False},
            ],
        }
        with mock.patch("frappe.new_doc", side_effect=lambda dt: created.append(FakeDoc(dt)) or created[-1]), \
                mock.patch("frappe.db.commit"):
            imp._save_extra_addresses("ACME", "Customer", record, result)
            imp._save_extra_contacts("ACME", "Customer", record, result)

        addresses = [d for d in created if d.doctype == "Address"]
        contacts = [d for d in created if d.doctype == "Contact"]
        self.assertEqual(len(addresses), 2)
        self.assertEqual(addresses[0].address_type, "Warehouse")
        self.assertEqual(addresses[0].address_title, "ACME - Warehouse")
        self.assertEqual(addresses[1].address_type, "Other")     # "Branch Office" not a known type
        self.assertEqual(len(contacts), 2)
        self.assertEqual(contacts[0].first_name, "Accounts")
        self.assertEqual(contacts[0].tables["phone_nos"][0]["is_primary_mobile_no"], 1)  # WhatsApp default
        self.assertEqual(contacts[1].tables["phone_nos"][0]["is_primary_mobile_no"], 0)

    def test_address_kept_without_pincode_on_state_mismatch(self):
        """India Compliance rejects a pincode whose digits don't match the state. The
        importer must keep the address (dropping just the PIN) instead of losing it -
        and never crash. Regression for the PIN/state warning aborting the migration."""
        from unittest import mock
        from tally_migrator.erpnext.importers import CustomerImporter, ImportResult

        class FakeAddr:
            def __init__(self):
                self.links = []
            def append(self, t, r):
                self.links.append(r)
            def insert(self, **k):
                if getattr(self, "pincode", ""):   # IC rejects the bad PIN
                    raise Exception("Postal Code 166982 is not associated with Kerala")
                self.inserted = True

        fake = FakeAddr()
        imp = CustomerImporter("_TMTest Co", "TC")
        imp._resolve_state = lambda d: "Kerala"
        result = ImportResult("Customer")
        rec = {"Address": "3 Marine Drive", "MailingName": "X", "PinCode": "166982",
               "CountryName": "India", "GSTRegistrationNumber": ""}
        with mock.patch("frappe.new_doc", return_value=fake), \
                mock.patch("frappe.db.commit"), mock.patch("frappe.db.rollback"), \
                mock.patch("frappe.log_error"):
            imp._save_address("X", "Customer", rec, result)

        self.assertTrue(getattr(fake, "inserted", False))   # address still created
        self.assertEqual(fake.pincode, "")                  # only the PIN was dropped
        self.assertEqual(len(result.errors), 0)             # no hard failure
        self.assertTrue(any("without its PIN" in w["reason"] for w in result.warnings))

    def test_partial_opening_rerun_posts_only_missing_batches(self):
        """Re-running after a partial failure must post the batches that never
        posted, not skip everything. Regression for C-A: the all-or-nothing guard
        used to abandon un-posted balances on re-run."""
        from tally_migrator.erpnext.importers import OpeningBalanceImporter
        from tally_migrator.tally.extractors import AccountNode

        imp = OpeningBalanceImporter("_TMTest Co", "TC")
        # A prior run posted the Asset batch only (Equity failed last time).
        imp._existing_opening_state = lambda: (False, {"Asset"})
        # Make account reference checks pass: any non-empty batch yields one line, so
        # BOTH an Asset and an Equity batch are built (run() builds a batch only when
        # _account_lines returns rows). The Asset batch then hits the already-posted
        # skip path; the Equity batch is pending and posts.
        imp._account_lines = lambda accounts, result: (
            [{"account": "x", "debit_in_account_currency": 100.0,
              "credit_in_account_currency": 0.0}] if accounts else []
        )
        imp._party_lines = lambda *a, **k: []
        posted_labels = []
        imp._post_batch = lambda label, lines, pd, result: posted_labels.append(label)

        asset = AccountNode(name="Cash", parent="", is_group=False,
                            root_type="Asset", account_type="", is_reserved=False,
                            opening_balance=100.0, opening_dr_cr="Dr")
        equity = AccountNode(name="Share Capital", parent="", is_group=False,
                             root_type="Equity", account_type="", is_reserved=False,
                             opening_balance=100.0, opening_dr_cr="Cr")
        result = imp.run(accounts=[asset, equity], customers=[], suppliers=[],
                         posting_date="2024-04-01")
        # Equity is posted now; Asset is recognised as already done and skipped.
        self.assertEqual(posted_labels, ["Equity"])
        self.assertEqual(result.skipped, 1)
        self.assertTrue(any("already posted" in w["reason"] for w in result.warnings))

    def test_batch_label_roundtrips_through_remark(self):
        """The label written to user_remark must parse back to the same label, and a
        foreign remark must not be mistaken for one of ours."""
        from tally_migrator.erpnext.importers import OpeningBalanceImporter as OBI
        for label in ("Asset", "Customer", "Supplier"):
            remark = f"{OBI._REMARK_PREFIX}{label})"
            self.assertEqual(OBI._batch_label_from_remark(remark), label)
        self.assertEqual(OBI._batch_label_from_remark("Manual opening entry"), "")
        self.assertEqual(OBI._batch_label_from_remark(None), "")

    def test_advance_payment_entry_is_idempotent_on_rerun(self):
        """A second run must NOT create a second advance Payment Entry.

        Regression for the doubling bug: Payment Entry.set_remarks() overwrites
        ``remarks`` with auto-text on save, which silently wiped the idempotency
        marker - so the re-run guard (``_existing_markers``) could not see the
        advance and re-posted it, doubling the Debtors/Creditors advance offset.
        ``custom_remarks=1`` makes ERPNext keep our marker, restoring idempotency.
        """
        require_company()
        from tally_migrator.erpnext.importers import PartyOpeningImporter
        from tally_migrator.tally.extractors import BillAllocation

        company = self.company
        abbr = frappe.get_cached_value("Company", company, "abbr")
        cust = "_TMTest Adv Cust"
        if not frappe.db.exists("Customer", {"customer_name": cust}):
            frappe.get_doc({
                "doctype": "Customer", "customer_name": cust,
                "customer_group": frappe.db.get_value(
                    "Customer Group", {"is_group": 0}, "name") or "All Customer Groups",
                "territory": frappe.db.get_value(
                    "Territory", {"is_group": 0}, "name") or "All Territories",
            }).insert(ignore_permissions=True)
            frappe.db.commit()

        # A customer credit (advance) opening posts an unallocated Payment Entry.
        bills = [BillAllocation(party=cust, bill_no="ADV-1", bill_date="2026-04-01",
                                amount=5000.0, dr_cr="Cr", is_advance=True)]
        customers = [{"_name": cust, "OpeningBalance": "5000 Cr", "CurrencyName": "INR"}]
        marker_like = f"Tally opening: {cust}%"

        from tally_migrator.migration import reconciliation as recon
        rec_acc = frappe.get_cached_value("Company", company, "default_receivable_account")

        def _run():
            return PartyOpeningImporter(company, abbr, "2026-04-01").run(bills, customers, [])

        try:
            bal_before = recon._opening_account_balance(company, rec_acc)
            r1 = _run()
            bal_after_1 = recon._opening_account_balance(company, rec_acc)
            r2 = _run()   # re-run: must skip, not duplicate
            bal_after_2 = recon._opening_account_balance(company, rec_acc)
            pes = frappe.get_all(
                "Payment Entry",
                filters={"company": company, "remarks": ["like", marker_like], "docstatus": 1},
                fields=["name", "custom_remarks", "remarks"])
            self.assertEqual(r1.created, 1, r1.errors)
            self.assertEqual(r2.created, 0)
            self.assertGreaterEqual(r2.skipped, 1)
            self.assertEqual(len(pes), 1,
                             f"advance Payment Entry doubled on re-run: {[p.name for p in pes]}")
            # The marker must survive on the saved doc - the actual fix.
            self.assertEqual(pes[0].custom_remarks, 1)
            self.assertTrue((pes[0].remarks or "").startswith("Tally opening:"))
            # Reconciliation read-back: the advance must be COUNTED (the figure moved
            # off its starting point) and IDEMPOTENT (the re-run does not change it).
            # This is the layer that masked the doubling bug - the advance is matched
            # by its remarks marker, which set_remarks() used to wipe.
            self.assertNotEqual(bal_after_1, bal_before,
                                "reconciliation did not pick up the advance opening")
            self.assertEqual(bal_after_1, bal_after_2,
                             f"reconciliation figure changed on re-run: "
                             f"{bal_after_1} -> {bal_after_2}")
        finally:
            for pe in frappe.get_all(
                    "Payment Entry",
                    filters={"company": company, "remarks": ["like", marker_like]},
                    pluck="name"):
                doc = frappe.get_doc("Payment Entry", pe)
                if doc.docstatus == 1:
                    doc.cancel()
                frappe.delete_doc("Payment Entry", pe, force=True, ignore_permissions=True)
            frappe.db.commit()

    def test_opening_stock_skipped_when_reconciliation_exists(self):
        """A second run must NOT post a second Opening Stock reconciliation."""
        from tally_migrator.erpnext.importers import StockOpeningImporter

        imp = StockOpeningImporter("_TMTest Co", "TC")
        imp._existing_opening_stock = lambda: True   # simulate a prior run
        result = imp.run(items=[{"_name": "X", "OpeningBalance": "55 Nos"}],
                         posting_date="2024-04-01")
        self.assertEqual(result.created, 0)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.warned, 1)
        self.assertIn("already posted", result.warnings[0]["reason"])

    def test_default_warehouse_skips_other_company_global_default(self):
        """Stock Settings' default_warehouse is global. On a multi-company site it
        can point at another company's warehouse; using it makes the Opening Stock
        reconciliation fail ('Warehouse X does not belong to company Y'). The lookup
        must be company-scoped and fall through to our own warehouse."""
        from unittest import mock
        from tally_migrator.erpnext.importers import StockOpeningImporter

        imp = StockOpeningImporter("_TMTest Co", "TC")
        calls = []

        def fake_exists(_doctype, filt):
            calls.append(filt)
            name = filt.get("name", "")
            if name == "Stores - FT":          # the global default - other company
                return filt.get("company") in (None, "FT")
            return name.endswith(" - TC")      # our company's migrated warehouse

        with mock.patch("frappe.db.get_single_value", return_value="Stores - FT"), \
                mock.patch("frappe.db.exists", side_effect=fake_exists):
            wh = imp._default_warehouse()

        self.assertNotEqual(wh, "Stores - FT")          # must not grab other company's
        self.assertTrue(wh.endswith(" - TC"))           # company-scoped fallback wins
        # the Stock Settings default was looked up *with* a company filter
        self.assertTrue(any(c.get("name") == "Stores - FT" and "company" in c
                            for c in calls))

    def test_zero_valuation_opening_stock_warns(self):
        """A positive opening qty with no rate/standard cost still posts, but warns
        that it carries zero book value (M-B)."""
        from unittest import mock
        from tally_migrator.erpnext.importers import StockOpeningImporter

        imp = StockOpeningImporter("_TMTest Co", "TC")
        imp._existing_opening_stock = lambda: False
        imp._default_warehouse = lambda: "Stores - TC"
        # Stop before the actual submit; we only assert the warning was raised.
        with mock.patch("frappe.get_doc", side_effect=Exception("stop")):
            result = imp.run(
                items=[{"_name": "NoRate", "OpeningBalance": "10 Nos",
                        "OpeningRate": "", "StandardCost": ""}],
                posting_date="2024-04-01")
        self.assertTrue(any("zero valuation" in w["reason"] for w in result.warnings))

    def test_negative_opening_stock_warns_and_is_not_posted(self):
        """A negative opening quantity can't go into an Opening Stock reconciliation;
        it must be dropped with a warning, not silently or fatally. (H-4)"""
        from tally_migrator.erpnext.importers import StockOpeningImporter

        imp = StockOpeningImporter("_TMTest Co", "TC")
        imp._existing_opening_stock = lambda: False
        imp._default_warehouse = lambda: "Stores - TC"
        result = imp.run(items=[{"_name": "Oversold", "OpeningBalance": "-5 Nos"}],
                         posting_date="2024-04-01")
        self.assertEqual(result.created, 0)        # nothing posted
        self.assertEqual(result.warned, 1)
        self.assertIn("negative", result.warnings[0]["reason"].lower())

    def test_duplicate_items_deduped_into_one_row(self):
        """A masters export can list the same Stock Item twice; both rows would land
        in one warehouse and trip 'Same item and warehouse combination should be
        unique', failing the whole document. Identical duplicates must collapse to a
        single row. (regression: opening stock posts nothing)"""
        from unittest import mock
        from tally_migrator.erpnext.importers import StockOpeningImporter

        imp = StockOpeningImporter("_TMTest Co", "TC")
        imp._existing_opening_stock = lambda: False
        imp._default_warehouse = lambda: "Stores - TC"
        captured = {}

        def fake_get_doc(d):
            captured["items"] = d["items"]
            raise Exception("stop")  # before insert/submit; we only inspect the rows

        with mock.patch("frappe.get_doc", side_effect=fake_get_doc):
            imp.run(
                items=[{"_name": "Pen", "OpeningBalance": "55 Nos"},
                       {"_name": "Pen", "OpeningBalance": "55 Nos"}],
                posting_date="2024-04-01")
        self.assertEqual(len(captured["items"]), 1)        # one row, not two
        self.assertEqual(captured["items"][0]["qty"], 55.0)  # not summed to 110

    def test_conflicting_duplicate_quantities_keep_larger_and_warn(self):
        """When the same item appears with different opening quantities, keep the
        larger and warn rather than silently picking one."""
        from unittest import mock
        from tally_migrator.erpnext.importers import StockOpeningImporter

        imp = StockOpeningImporter("_TMTest Co", "TC")
        imp._existing_opening_stock = lambda: False
        imp._default_warehouse = lambda: "Stores - TC"
        captured = {}

        def fake_get_doc(d):
            captured["items"] = d["items"]
            raise Exception("stop")

        with mock.patch("frappe.get_doc", side_effect=fake_get_doc):
            result = imp.run(
                items=[{"_name": "Pen", "OpeningBalance": "55 Nos"},
                       {"_name": "Pen", "OpeningBalance": "60 Nos"}],
                posting_date="2024-04-01")
        self.assertEqual(len(captured["items"]), 1)
        self.assertEqual(captured["items"][0]["qty"], 60.0)
        self.assertTrue(any("more than once" in w["reason"] for w in result.warnings))

    def test_opening_rate_with_unit_suffix_is_valued(self):
        """The valued case from real exports: OpeningRate '1.00/Nos' must produce a
        valuation_rate of 1.0, not 0 (which _to_float would give)."""
        from unittest import mock
        from tally_migrator.erpnext.importers import StockOpeningImporter

        imp = StockOpeningImporter("_TMTest Co", "TC")
        imp._existing_opening_stock = lambda: False
        imp._default_warehouse = lambda: "Stores - TC"
        captured = {}

        def fake_get_doc(d):
            captured["items"] = d["items"]
            raise Exception("stop")

        with mock.patch("frappe.get_doc", side_effect=fake_get_doc):
            imp.run(
                items=[{"_name": "Wireless Mouse", "OpeningBalance": "100 Nos",
                        "OpeningRate": "1.00/Nos", "OpeningValue": "-100.00"}],
                posting_date="2024-04-01")
        self.assertEqual(captured["items"][0]["valuation_rate"], 1.0)

    def test_opening_value_fallback_when_no_rate(self):
        """With no parseable rate but an OpeningValue present, valuation_rate falls
        back to |value| ÷ qty (sign is direction, not magnitude)."""
        from unittest import mock
        from tally_migrator.erpnext.importers import StockOpeningImporter

        imp = StockOpeningImporter("_TMTest Co", "TC")
        imp._existing_opening_stock = lambda: False
        imp._default_warehouse = lambda: "Stores - TC"
        captured = {}

        def fake_get_doc(d):
            captured["items"] = d["items"]
            raise Exception("stop")

        with mock.patch("frappe.get_doc", side_effect=fake_get_doc):
            imp.run(
                items=[{"_name": "Widget", "OpeningBalance": "10 Nos",
                        "OpeningRate": "", "OpeningValue": "-250.00"}],
                posting_date="2024-04-01")
        self.assertEqual(captured["items"][0]["valuation_rate"], 25.0)

    def test_zero_rate_rows_allow_zero_valuation(self):
        """A zero-rate opening row must set allow_zero_valuation_rate, or ERPNext
        rejects the whole reconciliation ('Valuation Rate required for Item …').
        Items Tally carries no value for post faithfully as qty-only."""
        from unittest import mock
        from tally_migrator.erpnext.importers import StockOpeningImporter

        imp = StockOpeningImporter("_TMTest Co", "TC")
        imp._existing_opening_stock = lambda: False
        imp._default_warehouse = lambda: "Stores - TC"
        captured = {}

        def fake_get_doc(d):
            captured["items"] = d["items"]
            raise Exception("stop")

        with mock.patch("frappe.get_doc", side_effect=fake_get_doc):
            imp.run(
                items=[{"_name": "Envelope", "OpeningBalance": "55 Nos"},
                       {"_name": "Mouse", "OpeningBalance": "100 Nos",
                        "OpeningRate": "1.00/Nos"}],
                posting_date="2024-04-01")
        rows = {r["item_code"]: r for r in captured["items"]}
        self.assertEqual(rows["Envelope"].get("allow_zero_valuation_rate"), 1)
        self.assertNotIn("allow_zero_valuation_rate", rows["Mouse"])  # has a rate

    def test_zero_valuation_warns_per_item_with_identical_text(self):
        """The importer emits one warning per zero-rate item, all with byte-identical
        text - the log's error table is what collapses them (test_collapse_identical),
        so a generic mechanism handles every repeated message, not just this one."""
        from unittest import mock
        from tally_migrator.erpnext.importers import StockOpeningImporter

        imp = StockOpeningImporter("_TMTest Co", "TC")
        imp._existing_opening_stock = lambda: False
        imp._default_warehouse = lambda: "Stores - TC"
        with mock.patch("frappe.get_doc", side_effect=Exception("stop")):
            result = imp.run(
                items=[{"_name": f"Item{i}", "OpeningBalance": "5 Nos"}
                       for i in range(4)],
                posting_date="2024-04-01")
        zero = [w for w in result.warnings if "zero valuation" in w["reason"]]
        self.assertEqual(len(zero), 4)                       # one per item
        self.assertEqual(len({w["reason"] for w in zero}), 1)  # identical text

    def test_value_rate_mismatch_warns(self):
        """When Tally carries BOTH an opening rate and value that don't satisfy
        value = qty x rate, the divergence is flagged (Item Master Rule 1)."""
        from unittest import mock
        from tally_migrator.erpnext.importers import StockOpeningImporter

        imp = StockOpeningImporter("_TMTest Co", "TC")
        imp._existing_opening_stock = lambda: False
        imp._default_warehouse = lambda: "Stores - TC"
        with mock.patch("frappe.get_doc", side_effect=Exception("stop")):
            result = imp.run(
                # 10 x 5 = 50, but Tally reports a value of 999 -> mismatch.
                items=[{"_name": "Widget", "OpeningBalance": "10 Nos",
                        "OpeningRate": "5.00/Nos", "OpeningValue": "-999.00"}],
                posting_date="2024-04-01")
        self.assertTrue(any("does not reconcile" in w["reason"] for w in result.warnings))

    def test_value_rate_consistent_does_not_warn(self):
        """The normal case: value = qty x rate (with Tally's Dr-negative value sign)
        must NOT warn, or every stock item would be flagged."""
        from unittest import mock
        from tally_migrator.erpnext.importers import StockOpeningImporter

        imp = StockOpeningImporter("_TMTest Co", "TC")
        imp._existing_opening_stock = lambda: False
        imp._default_warehouse = lambda: "Stores - TC"
        with mock.patch("frappe.get_doc", side_effect=Exception("stop")):
            result = imp.run(
                items=[{"_name": "Widget", "OpeningBalance": "10 Nos",
                        "OpeningRate": "5.00/Nos", "OpeningValue": "-50.00"}],
                posting_date="2024-04-01")
        self.assertFalse(any("does not reconcile" in w["reason"] for w in result.warnings))

    # ── Opening-balance batching (no DB - residual maths only) ──────────────────

    def test_warn_residual_only_fires_above_threshold(self):
        """The aggregate 'did not net to zero' warning is raised once, and only for
        a non-trivial residual (per-batch plugs must not each warn). (H-1)"""
        from tally_migrator.erpnext.importers import OpeningBalanceImporter, ImportResult

        imp = OpeningBalanceImporter("_TMTest Co", "TC")
        big = ImportResult("Journal Entry")
        imp._warn_residual(
            [{"debit_in_account_currency": 9000.0, "credit_in_account_currency": 0.0}], big)
        self.assertEqual(big.warned, 1)

        tiny = ImportResult("Journal Entry")
        imp._warn_residual(
            [{"debit_in_account_currency": 0.4, "credit_in_account_currency": 0.0}], tiny)
        self.assertEqual(tiny.warned, 0)   # below PLUG_NOISE_THRESHOLD
