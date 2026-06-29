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

    def test_party_side_effect_hooks_suspended_and_restored(self):
        """The per-contact Google-Contacts/Call-Log hooks are neutralised inside the
        context and restored after - the mechanism Step 5 relies on to drop the
        data-irrelevant per-record round-trips on large books."""
        import importlib
        from tally_migrator.erpnext.importers.party import (
            _party_side_effect_hooks_suspended, _SUSPENDED_PARTY_HOOKS)

        calls = []
        saved = []
        for mod_path, names in _SUSPENDED_PARTY_HOOKS:
            mod = importlib.import_module(mod_path)
            for nm in names:
                saved.append((mod, nm, getattr(mod, nm)))
                setattr(mod, nm, lambda *a, **k: calls.append(nm))
        try:
            with _party_side_effect_hooks_suspended():
                for mod_path, names in _SUSPENDED_PARTY_HOOKS:
                    mod = importlib.import_module(mod_path)
                    for nm in names:
                        getattr(mod, nm)("doc")           # suspended -> must be a no-op
            self.assertEqual(calls, [], "hooks must be neutralised inside the context")
            for mod_path, names in _SUSPENDED_PARTY_HOOKS:   # restored afterwards
                mod = importlib.import_module(mod_path)
                for nm in names:
                    getattr(mod, nm)("doc")
            self.assertTrue(calls, "hooks must be restored after the context")
        finally:
            for mod, nm, fn in saved:
                setattr(mod, nm, fn)

    def test_party_import_does_not_fire_contact_integration_hooks(self):
        """A normal Contact insert fires the Google-Contacts/Call-Log hooks; importing
        a party that creates a Contact must not (they are suspended), while the Contact
        is still created identically."""
        import importlib
        from tally_migrator.erpnext.importers.party import _SUSPENDED_PARTY_HOOKS

        calls = {"n": 0}
        saved = []
        for mod_path, names in _SUSPENDED_PARTY_HOOKS:
            try:
                mod = importlib.import_module(mod_path)
            except Exception:
                continue
            for nm in names:
                if hasattr(mod, nm):
                    saved.append((mod, nm, getattr(mod, nm)))
                    setattr(mod, nm, lambda *a, **k: calls.__setitem__("n", calls["n"] + 1))
        try:
            # Anchor: a plain Contact insert DOES fire the hooks, so "0 during import"
            # below is a real assertion and not a hook that simply never runs here.
            anchor = frappe.new_doc("Contact")
            anchor.first_name = "_TMTest Hook Anchor"
            anchor.append("phone_nos", {"phone": "9811111111", "is_primary_mobile_no": 1})
            anchor.insert(ignore_permissions=True)
            self.assertGreater(calls["n"], 0, "the hooks should fire on a normal Contact insert")

            fired_before = calls["n"]
            self.importer.import_customers([{
                "_name": "_TMTest Hook Cust", "LedgerMobile": "9822222222"}])
            self.assertEqual(
                calls["n"], fired_before,
                "the integration hooks must not fire during a party import")
            self.assertTrue(
                frappe.db.exists("Contact", {"first_name": "_TMTest Hook Cust"}),
                "the contact must still be created")
        finally:
            for mod, nm, fn in saved:
                setattr(mod, nm, fn)

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

    # ── Step 1: recover address/contact drops (no-state, bad email) ────────────

    def test_resolve_state_prefers_ledger_then_gstin_then_pin(self):
        """State resolves most-reliable first: Tally ledger state, else GSTIN state
        code, else derived from the PIN. Tally rarely sets a ledger state, so the
        PIN fallback is what keeps most addresses out of the missing-state drop."""
        from tally_migrator.erpnext.importers import CustomerImporter
        imp = CustomerImporter("_TMTest Co", "TC")
        # ledger state wins over everything
        self.assertEqual(imp._resolve_state(
            {"LedgerState": "Karnataka", "GSTRegistrationNumber": "27AAACT2727Q1ZW",
             "PinCode": "600001"}), "Karnataka")
        # no ledger state -> GSTIN's state code (27 -> Maharashtra) beats the PIN
        self.assertEqual(imp._resolve_state(
            {"GSTRegistrationNumber": "27AAACT2727Q1ZW", "PinCode": "600001"}),
            "Maharashtra")
        # only a PIN -> derived from it (600xxx -> Tamil Nadu)
        self.assertEqual(imp._resolve_state({"PinCode": "600001"}), "Tamil Nadu")
        # no signal at all -> empty (caller decides to skip)
        self.assertEqual(imp._resolve_state({}), "")

    def test_address_state_derived_from_pincode(self):
        """A party with a PIN but no ledger state / GSTIN keeps its address - the
        state is derived from the PIN, not dropped."""
        customer = {
            "_name": "_TMTest Customer PinState",
            "Address": "1 Pin Road",
            "PinCode": "560001",   # Bengaluru -> Karnataka
        }
        self.importer.import_customers([customer])
        addr = frappe.get_all(
            "Address", filters={"address_title": "_TMTest Customer PinState"},
            fields=["name", "state"])
        self.assertEqual(len(addr), 1, msg="address dropped despite a derivable state")
        self.assertEqual(addr[0].state, "Karnataka")

    def test_address_skipped_cleanly_when_no_state_signal(self):
        """No ledger state, GSTIN or PIN -> the Indian address cannot satisfy India
        Compliance, so it is skipped as a warning (the party still imports) WITHOUT a
        failed insert or an Error Log entry - the big-import flood we removed."""
        from unittest import mock
        if not frappe.get_meta("Address").has_field("gst_state"):
            self.skipTest("state-mandatory rule requires India Compliance")
        customer = {"_name": "_TMTest Customer NoState", "Address": "9 Nowhere Lane"}
        with mock.patch("frappe.log_error") as logged:
            result = self.importer.import_customers([customer])
        self.assertEqual(result.failed, 0)
        self.assertTrue(
            frappe.db.exists("Customer", {"customer_name": "_TMTest Customer NoState"}))
        self.assertFalse(
            frappe.db.exists("Address", {"address_title": "_TMTest Customer NoState"}))
        self.assertTrue(any("no state" in w["reason"] for w in result.warnings))
        logged.assert_not_called()

    def test_address_kept_when_email_invalid(self):
        """A malformed Tally email must not sink the whole address - the address
        imports, just without the bad email."""
        customer = {
            "_name": "_TMTest Customer BadEmailAddr",
            "Address": "2 Email Road",
            "LedgerState": "Maharashtra",
            "LedgerEmail": "NA",   # not a valid email address
        }
        self.importer.import_customers([customer])
        addr = frappe.get_all(
            "Address", filters={"address_title": "_TMTest Customer BadEmailAddr"},
            fields=["name", "email_id"])
        self.assertEqual(len(addr), 1, msg="address dropped over a bad email")
        self.assertFalse(addr[0].email_id)

    def test_contact_keeps_phone_when_email_invalid(self):
        """A malformed email is dropped but the phone-only contact is still created,
        and a warning records the dropped email."""
        customer = {
            "_name": "_TMTest Customer BadEmail",
            "LedgerMobile": "9812345678",
            "LedgerEmail": "n/a",
        }
        result = self.importer.import_customers([customer])
        doc = frappe.get_doc("Contact", {"first_name": "_TMTest Customer BadEmail"})
        self.assertEqual([e.email_id for e in doc.email_ids], [])
        self.assertIn("9812345678", [p.phone for p in doc.phone_nos])
        self.assertTrue(any("email" in w["reason"] for w in result.warnings))

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

    # ── Batch / expiry / MRP (Tier 2) ───────────────────────────────────────────

    @staticmethod
    def _batch_item(name="_TMTest Batch Item"):
        return {
            "_name": name, "Parent": "All Item Groups", "BaseUnits": "Nos",
            "HSNCode": "99041010",
            "IsBatchWiseOn": "Yes", "IsPerishableOn": "Yes", "HasMfgDate": "Yes",
            "Mrp": "50000.00/Nos",
            "OpeningBalance": "90 Nos",
            "GodownOpenings": [{
                "godown": "Main Location", "qty": "90 Nos", "rate": "50000.00/Nos",
                "value": "-4500000.00", "batch": "_TMTest BATCH1",
                "mfg_date": "20260501", "expiry": "31-Jul-26",
            }],
        }

    def test_batch_item_sets_has_batch_no_and_expiry(self):
        item = self._batch_item()
        result = self.importer.import_items([item])
        self.assertEqual(result.failed, 0, msg=str(result.errors))
        code = frappe.db.get_value("Item", {"item_name": item["_name"]}, "name")
        flags = frappe.db.get_value(
            "Item", code, ["has_batch_no", "has_expiry_date"], as_dict=True)
        self.assertEqual(flags.has_batch_no, 1)
        self.assertEqual(flags.has_expiry_date, 1)

    def test_batch_master_created_with_dates(self):
        item = self._batch_item()
        self.importer.import_items([item])
        self.addCleanup(
            lambda: frappe.db.exists("Batch", "_TMTest BATCH1")
            and frappe.delete_doc("Batch", "_TMTest BATCH1", force=1))
        res = self.importer.import_batches([item])
        self.assertEqual(res.failed, 0, msg=str(res.errors))
        b = frappe.db.get_value(
            "Batch", "_TMTest BATCH1",
            ["item", "manufacturing_date", "expiry_date"], as_dict=True)
        self.assertTrue(b)
        self.assertEqual(str(b.manufacturing_date), "2026-05-01")
        self.assertEqual(str(b.expiry_date), "2026-07-31")

    def test_batch_id_scoping_for_shared_names(self):
        """shared_batch_names + batch_id_for: a name used by >1 item is scoped per
        item; a unique name is kept verbatim; the scoped id stays within 140 chars."""
        from tally_migrator.naming import shared_batch_names, batch_id_for
        items = [
            {"_name": "A", "IsBatchWiseOn": "Yes",
             "GodownOpenings": [{"batch": "169.49"}, {"batch": "UNIQ-A"}]},
            {"_name": "B", "IsBatchWiseOn": "Yes",
             "GodownOpenings": [{"batch": "169.49"}]},
        ]
        shared = shared_batch_names(items)
        self.assertEqual(shared, {"169.49"})
        self.assertEqual(batch_id_for("169.49", "A", shared), "169.49 - A")
        self.assertEqual(batch_id_for("169.49", "B", shared), "169.49 - B")
        self.assertEqual(batch_id_for("UNIQ-A", "A", shared), "UNIQ-A")     # unique
        self.assertLessEqual(len(batch_id_for("169.49", "Z" * 200, shared)), 140)

    def test_opening_row_uses_scoped_batch_no_for_shared_name(self):
        """A row for a shared batch name is tagged with the per-item scoped batch id
        (matching what the Batch importer created), not the bare colliding name."""
        from tally_migrator.erpnext.importers import StockOpeningImporter
        from tally_migrator.erpnext.importers.base import ImportResult
        imp = StockOpeningImporter("_TMTest Co", "TC")
        imp._shared_batch_names = {"169.49"}
        by_key, res = {}, ImportResult("Stock Reconciliation")
        imp._add_opening_row(by_key, "ItemA", "WH", "5 Nos", "1/Nos", "", None,
                             res, batch="169.49")
        self.assertEqual(next(iter(by_key.values()))["batch_no"], "169.49 - ItemA")

    def test_batch_importer_scopes_shared_batch_id_per_item(self):
        """Two items sharing one Tally batch name each get their OWN ERPNext Batch
        (scoped by item), instead of the second being skipped on the global id -
        the fix that lets every item's batch-tracked opening stock post."""
        from tally_migrator.naming import safe_item_code

        def bitem(name):
            it = self._batch_item(name)
            it["GodownOpenings"][0]["batch"] = "_TMTest SHAREDRATE"
            return it

        a, b = bitem("_TMTest BShare A"), bitem("_TMTest BShare B")
        code_a, code_b = safe_item_code("_TMTest BShare A"), safe_item_code("_TMTest BShare B")
        for c in (code_a, code_b):
            self.addCleanup(
                lambda cc=c: frappe.db.exists("Batch", f"_TMTest SHAREDRATE - {cc}")
                and frappe.delete_doc("Batch", f"_TMTest SHAREDRATE - {cc}", force=1))
        self.importer.import_items([a, b])
        res = self.importer.import_batches([a, b])
        self.assertEqual(res.failed, 0, msg=str(res.errors))
        self.assertTrue(frappe.db.exists("Batch", f"_TMTest SHAREDRATE - {code_a}"))
        self.assertTrue(frappe.db.exists("Batch", f"_TMTest SHAREDRATE - {code_b}"))

    def test_mrp_creates_item_price_on_mrp_list(self):
        item = self._batch_item()
        self.importer.import_items([item])
        code = frappe.db.get_value("Item", {"item_name": item["_name"]}, "name")
        self.addCleanup(
            lambda: [frappe.delete_doc("Item Price", p, force=1)
                     for p in frappe.get_all(
                         "Item Price", filters={"item_code": code}, pluck="name")])
        self.importer.import_prices([item])
        rows = frappe.get_all(
            "Item Price", filters={"item_code": code, "price_list": "MRP"},
            fields=["price_list_rate", "uom"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].price_list_rate, 50000.0)

    def test_opening_stock_row_carries_batch_no(self):
        from tally_migrator.erpnext.importers import StockOpeningImporter
        from tally_migrator.erpnext.importers.base import ImportResult
        imp = StockOpeningImporter("_TMTest Co", "TC")
        imp._warehouse_for_godown = lambda g, d: d   # avoid DB warehouse lookup
        item = self._batch_item()
        placements = imp._placements(item, "WH - TC")
        self.assertEqual(placements[0][4], "_TMTest BATCH1")   # batch in the tuple
        res, by_key = ImportResult("Stock Reconciliation"), {}
        for wh, q, r, v, batch in placements:
            imp._add_opening_row(by_key, "X", wh, q, r, v, None, res, batch=batch)
        row = next(iter(by_key.values()))
        self.assertEqual(row["batch_no"], "_TMTest BATCH1")
        self.assertEqual(row["qty"], 90.0)
        # ERPNext v15+ throws "Please add Serial and Batch Bundle" on a batch row
        # unless use_serial_batch_fields is set; with it, on_submit builds the bundle
        # from batch_no. Without this the whole opening-stock reconciliation fails.
        self.assertEqual(row["use_serial_batch_fields"], 1)

    def test_ensure_serial_batch_enabled_flips_setting_when_off(self):
        from unittest import mock
        from tally_migrator.erpnext.importers import StockOpeningImporter
        from tally_migrator.erpnext.importers.base import ImportResult
        from tally_migrator.erpnext.importers import openings
        res = ImportResult("Stock Reconciliation")
        with mock.patch.object(openings.frappe.db, "get_single_value", return_value=0), \
             mock.patch.object(openings.frappe.db, "set_single_value") as set_single:
            StockOpeningImporter._ensure_serial_batch_enabled(res)
        set_single.assert_called_once_with(
            "Stock Settings", "enable_serial_and_batch_no_for_item", 1)
        self.assertTrue(res.warnings)   # the flip is recorded on the log

    def test_ensure_serial_batch_enabled_noop_when_already_on(self):
        from unittest import mock
        from tally_migrator.erpnext.importers import StockOpeningImporter
        from tally_migrator.erpnext.importers.base import ImportResult
        from tally_migrator.erpnext.importers import openings
        res = ImportResult("Stock Reconciliation")
        with mock.patch.object(openings.frappe.db, "get_single_value", return_value=1), \
             mock.patch.object(openings.frappe.db, "set_single_value") as set_single:
            StockOpeningImporter._ensure_serial_batch_enabled(res)
        set_single.assert_not_called()
        self.assertFalse(res.warnings)

    def test_parse_expiry_formats(self):
        from tally_migrator.erpnext.importers.batch import BatchImporter
        self.assertEqual(BatchImporter._parse_expiry("31-Jul-26"), "2026-07-31")
        self.assertEqual(BatchImporter._parse_expiry(""), "")
        self.assertEqual(BatchImporter._parse_expiry("garbage"), "")

    def test_non_batch_item_ignores_implicit_primary_batch(self):
        """Tally stamps EVERY item's opening with an implicit 'Primary Batch'. A
        non-batch item (IsBatchWiseOn=No) must NOT get batch_no on its opening row,
        or ERPNext rejects the row (has_batch_no=0)."""
        from tally_migrator.erpnext.importers import StockOpeningImporter
        from tally_migrator.erpnext.importers.base import ImportResult
        imp = StockOpeningImporter("_TMTest Co", "TC")
        imp._warehouse_for_godown = lambda g, d: d
        item = {"_name": "PlainItem", "IsBatchWiseOn": "No", "GodownOpenings": [
            {"godown": "Main Location", "qty": "10 Nos", "rate": "5.00/Nos",
             "value": "-50.00", "batch": "Primary Batch"}]}
        res, by_key = ImportResult("Stock Reconciliation"), {}
        for wh, q, r, v, batch in imp._placements(item, "WH - TC"):
            imp._add_opening_row(by_key, "PlainItem", wh, q, r, v, None, res, batch=batch)
        row = next(iter(by_key.values()))
        self.assertNotIn("batch_no", row)
        # No batch_no -> the bundle flag must stay off too (a plain qty/warehouse row).
        self.assertNotIn("use_serial_batch_fields", row)

    def test_batch_importer_skips_non_batch_items(self):
        """A non-batch item carrying an implicit 'Primary Batch' must not get a Batch."""
        item = {"_name": "_TMTest Plain", "IsBatchWiseOn": "No", "GodownOpenings": [
            {"godown": "Main Location", "qty": "10 Nos", "batch": "Primary Batch",
             "mfg_date": "", "expiry": ""}]}
        res = self.importer.import_batches([item])
        self.assertEqual(res.created, 0)

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

    def test_colliding_group_keeps_ledger_and_rehomes_children(self):
        """When a Tally group's name collides (case-insensitively) with an existing
        ledger, the importer must NOT convert that ledger - it may be load-bearing
        (e.g. India Compliance wires "TDS Payable" into every Tax Withholding
        Category). The ledger stays a ledger, and the Tally group's children are
        re-homed to the ledger's own parent so they still import.

        Regression for the Frontier issue where promoting a standard ledger to a group
        broke its opening entry and IC's tax-withholding setup."""
        require_company()
        from tally_migrator.erpnext.importers.accounts import AccountImporter
        from tally_migrator.tally.extractors import AccountNode

        abbr = frappe.get_value("Company", self.company, "abbr")
        root = frappe.db.get_value(
            "Account", {"company": self.company, "root_type": "Asset",
                        "is_group": 1, "parent_account": ["is", "not set"]}, "name")
        placeholder = f"_TMTest Equip - {abbr}"
        for nm in (f"_TMTest Camera - {abbr}", placeholder):
            if frappe.db.exists("Account", nm):
                frappe.delete_doc("Account", nm, force=True, ignore_permissions=True)
        # A pre-existing typed ledger (like ERPNext's standard CoA ships), under root.
        frappe.get_doc({
            "doctype": "Account", "account_name": "_TMTest Equip",
            "company": self.company, "parent_account": root,
            "is_group": 0, "root_type": "Asset", "account_type": "Fixed Asset",
        }).insert(ignore_permissions=True)

        imp = AccountImporter(self.company, abbr, mode="reuse")
        # Same name in a different case (the collision is case-insensitive in MariaDB),
        # plus a child that previously could only be created by promoting the ledger.
        group = AccountNode(name="_TMTEST EQUIP", parent="", is_group=True,
                            root_type="Asset", account_type="", is_reserved=False)
        child = AccountNode(name="_TMTest Camera", parent="_TMTEST EQUIP",
                            is_group=False, root_type="Asset", account_type="",
                            is_reserved=False)
        result = imp.run([group, child])

        self.assertEqual(result.failed, 0, msg=str(result.errors))
        # The colliding ledger is LEFT a ledger - never converted.
        self.assertEqual(
            frappe.db.get_value("Account", placeholder, "is_group"), 0,
            "the colliding standard ledger must NOT be converted to a group")
        # The child still imports, re-homed under the ledger's own parent (root here).
        child_name = f"_TMTest Camera - {abbr}"
        self.assertTrue(frappe.db.exists("Account", child_name),
                        "the child must still be created")
        self.assertEqual(
            frappe.db.get_value("Account", child_name, "parent_account"), root,
            "the child must nest under the existing ledger's parent")
        self.assertTrue(any("kept as a ledger" in w["reason"] for w in result.warnings))

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

    # ── Party group derivation (no DB - existence stubbed) ──────────────────────
    def test_customer_group_derived_from_tally_parent(self):
        """The customer's Tally group (its PARENT, e.g. 'Trade Debtors - Domestic')
        becomes its ERPNext Customer Group when that group exists - not the hardcoded
        default. (Tier-1 fix: groups were collapsed into 'Commercial'.)"""
        from unittest import mock
        from tally_migrator.erpnext.importers import CustomerImporter
        imp = CustomerImporter("_TMTest Co", "TC")
        with mock.patch("frappe.db.exists", return_value=True):
            doc = imp.build_doc({"_name": "Acme", "Parent": "Trade Debtors - Domestic"})
        self.assertEqual(doc["customer_group"], "Trade Debtors - Domestic")

    def test_supplier_group_derived_from_tally_parent(self):
        from unittest import mock
        from tally_migrator.erpnext.importers import SupplierImporter
        imp = SupplierImporter("_TMTest Co", "TC")
        with mock.patch("frappe.db.exists", return_value=True):
            doc = imp.build_doc({"_name": "Zeta", "Parent": "Trade Creditors - Goods"})
        self.assertEqual(doc["supplier_group"], "Trade Creditors - Goods")

    def test_party_group_falls_back_to_default_when_absent(self):
        """A blank Tally group, or one that couldn't be created, falls back to the
        standard default so the party always gets a valid (leaf) group."""
        from unittest import mock
        from tally_migrator.erpnext.importers import CustomerImporter
        from tally_migrator.tally.mappings import DEFAULT_CUSTOMER_GROUP
        imp = CustomerImporter("_TMTest Co", "TC")
        # blank parent -> default, no existence check needed
        self.assertEqual(imp.build_doc({"_name": "Acme", "Parent": ""})["customer_group"],
                         DEFAULT_CUSTOMER_GROUP)
        # parent set but group doesn't exist (creation failed) -> default
        with mock.patch("frappe.db.exists", return_value=False):
            doc = imp.build_doc({"_name": "Acme", "Parent": "Trade Debtors - Domestic"})
        self.assertEqual(doc["customer_group"], DEFAULT_CUSTOMER_GROUP)

    # ── Auto-created reference masters are tracked so revert removes them ────────
    def test_party_group_creation_is_tracked_for_revert(self):
        from unittest import mock
        from tally_migrator.erpnext.importers import CustomerImporter
        from tally_migrator.erpnext.importers.base import ImportResult
        imp = CustomerImporter("_TMTest Co", "TC")
        res = ImportResult("Customer")
        fake = mock.MagicMock(); fake.name = "Trade Debtors"
        with mock.patch("frappe.db.exists", return_value=False), \
             mock.patch("frappe.new_doc", return_value=fake), \
             mock.patch("frappe.db.commit"):
            imp._ensure_party_groups({"Trade Debtors"}, res)
        self.assertIn({"name": "Trade Debtors", "doctype": "Customer Group"}, res.created_docs)

    def test_item_group_creation_is_tracked_for_revert(self):
        from unittest import mock
        from tally_migrator.erpnext.importers import ItemImporter
        from tally_migrator.erpnext.importers.base import ImportResult
        imp = ItemImporter("_TMTest Co", "TC")
        res = ImportResult("Item")
        fake = mock.MagicMock(); fake.name = "Stationery"
        with mock.patch("frappe.db.exists", return_value=False), \
             mock.patch("frappe.new_doc", return_value=fake), \
             mock.patch("frappe.db.commit"):
            imp._ensure_item_groups({"Stationery"}, res)
        self.assertIn({"name": "Stationery", "doctype": "Item Group"}, res.created_docs)

    def test_bank_creation_is_tracked_for_revert(self):
        from unittest import mock
        from tally_migrator.erpnext.importers import banks
        from tally_migrator.erpnext.importers.base import ImportResult
        res = ImportResult("Account")
        with mock.patch.object(banks.frappe.db, "exists", return_value=False), \
             mock.patch.object(banks.frappe, "get_doc"), \
             mock.patch.object(banks.frappe.db, "commit"):
            out = banks._ensure_bank("HDFC Bank", res)
        self.assertEqual(out, "HDFC Bank")
        self.assertIn({"name": "HDFC Bank", "doctype": "Bank"}, res.created_docs)
        # An already-existing Bank must NOT be recorded (leave pre-existing data alone).
        res2 = ImportResult("Account")
        with mock.patch.object(banks.frappe.db, "exists", return_value=True):
            banks._ensure_bank("HDFC Bank", res2)
        self.assertEqual(res2.created_docs, [])

    def test_uom_category_creation_is_tracked_for_revert(self):
        from unittest import mock
        from tally_migrator.erpnext.importers import units
        from tally_migrator.erpnext.importers.units import UnitImporter
        from tally_migrator.erpnext.importers.base import ImportResult
        res = ImportResult("UOM")
        fake = mock.MagicMock(); fake.name = "Tally Imported"
        with mock.patch.object(units.frappe.db, "has_column", return_value=True), \
             mock.patch.object(units.frappe, "get_all", return_value=[]), \
             mock.patch.object(units.frappe, "get_doc", return_value=fake), \
             mock.patch.object(units.frappe.db, "commit"):
            UnitImporter._uom_category(res)
        self.assertIn({"name": "Tally Imported", "doctype": "UOM Category"}, res.created_docs)

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
            "_name": "ACME", "CountryName": "India", "LedgerState": "Karnataka",
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
        # Extra addresses inherit the party's state (India Compliance requires one on an
        # Indian address; the Tally address-book row carries none of its own).
        self.assertEqual(addresses[0].state, "Karnataka")
        self.assertEqual(addresses[1].state, "Karnataka")
        self.assertEqual(len(contacts), 2)
        self.assertEqual(contacts[0].first_name, "Accounts")
        self.assertEqual(contacts[0].tables["phone_nos"][0]["is_primary_mobile_no"], 1)  # WhatsApp default
        self.assertEqual(contacts[1].tables["phone_nos"][0]["is_primary_mobile_no"], 0)

    def test_extra_address_state_chain(self):
        """Extra-address state resolves most-precise first: the row's own state, else
        derived from the row's own pincode (IC map), else the party's state."""
        from tally_migrator.erpnext.importers import CustomerImporter
        imp = CustomerImporter("_TMTest Co", "TC")
        party = {"LedgerState": "Karnataka"}
        # 1. row carries its own state -> used verbatim (mapped)
        self.assertEqual(
            imp._extra_address_state({"state": "Maharashtra"}, party), "Maharashtra")
        # 2. no row state, but a row pincode -> derived from it (Chennai 600xxx -> TN),
        #    NOT the party's Karnataka
        self.assertEqual(
            imp._extra_address_state({"pincode": "600001"}, party), "Tamil Nadu")
        # 3. neither -> inherit the party's state
        self.assertEqual(imp._extra_address_state({}, party), "Karnataka")
        # pincode helper: too-short / unknown -> "" (never raises)
        self.assertEqual(imp._state_from_pincode("56"), "")
        self.assertEqual(imp._state_from_pincode("560001"), "Karnataka")

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

    def test_opening_balance_skips_account_that_is_a_group_in_erpnext(self):
        """A Tally ledger that exists in ERPNext as a GROUP (e.g. promoted so its
        sub-accounts could nest - see test_account_group_promotes_colliding_ledger)
        must be skipped from the opening Journal Entry, not added as a line. A group
        account cannot carry a balance, and one such line fails the whole class batch
        on submit, losing every opening balance in it. Regression for the Step 3 /
        opening-balance interaction."""
        require_company()
        from tally_migrator.erpnext.importers.openings import OpeningBalanceImporter
        from tally_migrator.erpnext.importers.base import ImportResult
        from tally_migrator.tally.extractors import AccountNode
        from tally_migrator.naming import company_scoped

        company = self.company
        abbr = frappe.get_value("Company", company, "abbr")
        root = frappe.db.get_value(
            "Account", {"company": company, "root_type": "Liability",
                        "is_group": 1, "parent_account": ["is", "not set"]}, "name")
        grp_base, led_base = "_TMTest Promoted Liab", "_TMTest Plain Liab"
        grp, led = company_scoped(grp_base, abbr), company_scoped(led_base, abbr)
        if not frappe.db.exists("Account", grp):
            frappe.get_doc({"doctype": "Account", "account_name": grp_base,
                            "company": company, "parent_account": root,
                            "is_group": 1, "root_type": "Liability"}).insert(ignore_permissions=True)
        if not frappe.db.exists("Account", led):
            frappe.get_doc({"doctype": "Account", "account_name": led_base,
                            "company": company, "parent_account": root,
                            "is_group": 0, "root_type": "Liability"}).insert(ignore_permissions=True)

        def node(name):
            return AccountNode(name=name, parent="", is_group=False, root_type="Liability",
                               account_type="", is_reserved=False,
                               opening_balance=1000.0, opening_dr_cr="Cr")

        res = ImportResult("Journal Entry")
        lines = OpeningBalanceImporter(company, abbr)._account_lines(
            [node(grp_base), node(led_base)], res)
        accounts = [l["account"] for l in lines]
        self.assertIn(led, accounts, "the ordinary ledger opening should still post")
        self.assertNotIn(grp, accounts, "the group account must be skipped")
        self.assertTrue(any("group account" in w["reason"] for w in res.warnings))

    def test_accounting_dimensions_cached_memoises_and_restores(self):
        """The opening-stock phase memoises ERPNext's per-item get_accounting_dimensions
        (a company-wide constant) to cut a DB round-trip per item, then restores it.
        The wrapper must hit the underlying lookup once, return a fresh copy each call
        (so a mutating caller can't corrupt the cache), and restore the original."""
        from tally_migrator.erpnext.importers import openings
        from erpnext.accounts.doctype.accounting_dimension import accounting_dimension as ad

        calls = {"n": 0}

        def spy(as_list=True):
            calls["n"] += 1
            return ["dim_a"]

        original = ad.get_accounting_dimensions
        ad.get_accounting_dimensions = spy
        try:
            with openings._accounting_dimensions_cached():
                r1 = ad.get_accounting_dimensions()
                r2 = ad.get_accounting_dimensions()
                r3 = ad.get_accounting_dimensions()
            self.assertEqual(calls["n"], 1, "the underlying lookup should run once")
            self.assertEqual(r1, ["dim_a"])
            self.assertIsNot(r1, r2, "each call should return a fresh copy")
            self.assertEqual(r3, ["dim_a"])
            self.assertIs(ad.get_accounting_dimensions, spy, "must restore on exit")
        finally:
            ad.get_accounting_dimensions = original

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

    def test_opening_stock_skips_items_already_posted(self):
        """Per-item idempotency: an item+warehouse already carried by a submitted
        opening reconciliation is not re-posted, so a re-run can resume the rest
        without doubling stock."""
        from tally_migrator.erpnext.importers import StockOpeningImporter
        from tally_migrator.naming import safe_item_code

        imp = StockOpeningImporter("_TMTest Co", "TC")
        imp._default_warehouse = lambda: "Stores - TC"
        imp._posted_keys = lambda: {(safe_item_code("X"), "Stores - TC")}
        result = imp.run(items=[{"_name": "X", "OpeningBalance": "55 Nos"}],
                         posting_date="2024-04-01")
        self.assertEqual(result.created, 0)
        self.assertEqual(result.skipped, 1)
        self.assertIn("already posted", result.warnings[-1]["reason"])

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
            captured.setdefault("items", d["items"])  # keep the first (full chunk)
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
            captured.setdefault("items", d["items"])  # keep the first (full chunk)
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
            captured.setdefault("items", d["items"])  # keep the first (full chunk)
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
            captured.setdefault("items", d["items"])  # keep the first (full chunk)
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
            captured.setdefault("items", d["items"])  # keep the first (full chunk)
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

    def test_non_stock_item_excluded_from_opening_stock(self):
        """A service / non-stock item carrying an opening quantity must be dropped
        from the aggregate Stock Reconciliation (with a warning) so that one bad row
        cannot fail the whole document and lose every item's opening stock."""
        from unittest import mock
        from tally_migrator.erpnext.importers import StockOpeningImporter
        from tally_migrator.naming import safe_item_code

        imp = StockOpeningImporter("_TMTest Co", "TC")
        imp._existing_opening_stock = lambda: False
        imp._default_warehouse = lambda: "Stores - TC"
        captured = {}

        def fake_get_doc(d):
            captured.setdefault("items", d["items"])  # keep the first (full chunk)
            raise Exception("stop")

        items = [
            {"_name": "_TMTest Service", "TypeOfSupply": "Services",
             "OpeningBalance": "5 Nos", "OpeningRate": "10/Nos"},
            {"_name": "_TMTest Goods", "OpeningBalance": "7 Nos", "OpeningRate": "3/Nos"},
        ]
        with mock.patch("frappe.get_doc", side_effect=fake_get_doc):
            result = imp.run(items=items, posting_date="2024-04-01")
        codes = [r["item_code"] for r in captured["items"]]
        self.assertIn(safe_item_code("_TMTest Goods"), codes)
        self.assertNotIn(safe_item_code("_TMTest Service"), codes)
        self.assertTrue(any("non-stock" in w["reason"] for w in result.warnings))

    def test_all_non_stock_items_post_nothing_cleanly(self):
        """When only service / non-stock items carry an opening quantity, nothing is
        posted (no Stock Reconciliation is even built) and the run does not fail."""
        from unittest import mock
        from tally_migrator.erpnext.importers import StockOpeningImporter

        imp = StockOpeningImporter("_TMTest Co", "TC")
        imp._existing_opening_stock = lambda: False
        imp._default_warehouse = lambda: "Stores - TC"
        called = {"n": 0}

        def fake_get_doc(d):
            called["n"] += 1
            raise Exception("should not be called")

        items = [{"_name": "_TMTest Svc Only", "TypeOfSupply": "Services",
                  "OpeningBalance": "9 Nos", "OpeningRate": "2/Nos"}]
        with mock.patch("frappe.get_doc", side_effect=fake_get_doc):
            result = imp.run(items=items, posting_date="2024-04-01")
        self.assertEqual(called["n"], 0)
        self.assertEqual(result.created, 0)
        self.assertEqual(result.failed, 0)
        self.assertTrue(any("non-stock" in w["reason"] for w in result.warnings))

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

    def test_opening_stock_splits_across_godowns(self):
        """An item whose opening stock spans two migrated godowns posts one row per
        warehouse (Tier-1 fix: it used to collapse into a single default warehouse)."""
        from unittest import mock
        from tally_migrator.erpnext.importers import StockOpeningImporter

        imp = StockOpeningImporter("_TMTest Co", "TC")
        imp._existing_opening_stock = lambda: False
        imp._default_warehouse = lambda: "Stores - TC"
        captured = {}

        def fake_get_doc(d):
            captured.setdefault("items", d["items"])  # keep the first (full chunk)
            raise Exception("stop")

        # Both godowns are "migrated" (warehouse exists); the default isn't consulted.
        with mock.patch("frappe.db.exists", return_value=True), \
                mock.patch("frappe.get_doc", side_effect=fake_get_doc):
            imp.run(items=[{
                "_name": "Pen", "OpeningBalance": "30 Nos",
                "GodownOpenings": [
                    {"godown": "Bangalore Godown", "qty": "20 Nos",
                     "rate": "10.00/Nos", "value": "-200.00"},
                    {"godown": "Delhi Godown", "qty": "10 Nos",
                     "rate": "10.00/Nos", "value": "-100.00"},
                ]}], posting_date="2024-04-01")
        rows = {r["warehouse"]: r for r in captured["items"]}
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows["Bangalore Godown - TC"]["qty"], 20.0)
        self.assertEqual(rows["Delhi Godown - TC"]["qty"], 10.0)
        self.assertEqual(rows["Bangalore Godown - TC"]["valuation_rate"], 10.0)

    def test_opening_stock_godown_falls_back_to_default_warehouse(self):
        """A godown that wasn't migrated (e.g. Tally's implicit 'Main Location')
        falls back to the default warehouse - so the current single-godown exports
        behave exactly as before."""
        from unittest import mock
        from tally_migrator.erpnext.importers import StockOpeningImporter

        imp = StockOpeningImporter("_TMTest Co", "TC")
        imp._existing_opening_stock = lambda: False
        imp._default_warehouse = lambda: "Stores - TC"
        captured = {}

        def fake_get_doc(d):
            captured.setdefault("items", d["items"])  # keep the first (full chunk)
            raise Exception("stop")

        # No warehouse exists for the godown -> fall back to the default.
        with mock.patch("frappe.db.exists", return_value=False), \
                mock.patch("frappe.get_doc", side_effect=fake_get_doc):
            imp.run(items=[{
                "_name": "Pen", "OpeningBalance": "30 Nos",
                "GodownOpenings": [
                    {"godown": "Main Location", "qty": "30 Nos",
                     "rate": "10.00/Nos", "value": "-300.00"},
                ]}], posting_date="2024-04-01")
        self.assertEqual(len(captured["items"]), 1)
        self.assertEqual(captured["items"][0]["warehouse"], "Stores - TC")
        self.assertEqual(captured["items"][0]["qty"], 30.0)

    def test_opening_stock_unmigrated_godowns_sum_into_default(self):
        """Two godowns that both fall back to the default warehouse are summed into
        one row (not flagged as a spurious duplicate)."""
        from unittest import mock
        from tally_migrator.erpnext.importers import StockOpeningImporter

        imp = StockOpeningImporter("_TMTest Co", "TC")
        imp._existing_opening_stock = lambda: False
        imp._default_warehouse = lambda: "Stores - TC"
        captured = {}

        def fake_get_doc(d):
            captured.setdefault("items", d["items"])  # keep the first (full chunk)
            raise Exception("stop")

        with mock.patch("frappe.db.exists", return_value=False), \
                mock.patch("frappe.get_doc", side_effect=fake_get_doc):
            result = imp.run(items=[{
                "_name": "Pen", "OpeningBalance": "30 Nos",
                "GodownOpenings": [
                    {"godown": "A", "qty": "20 Nos", "rate": "", "value": "-200.00"},
                    {"godown": "B", "qty": "10 Nos", "rate": "", "value": "-100.00"},
                ]}], posting_date="2024-04-01")
        self.assertEqual(len(captured["items"]), 1)
        self.assertEqual(captured["items"][0]["qty"], 30.0)           # 20 + 10
        self.assertEqual(captured["items"][0]["valuation_rate"], 10.0)  # 300 / 30
        self.assertFalse(any("more than once" in w["reason"] for w in result.warnings))

    # ── Godown-wise opening stock invariant (no DB - placement maths only) ──────

    def _godown_rows(self, item, default="WH - TC"):
        """Build the opening rows _placements + _add_opening_row produce for ``item``,
        with the godown→warehouse map stubbed (a named godown migrates to
        '<godown> - TC', a blank one falls to the default)."""
        from tally_migrator.erpnext.importers import StockOpeningImporter
        from tally_migrator.erpnext.importers.base import ImportResult
        imp = StockOpeningImporter("_TMTest Co", "TC")
        imp._warehouse_for_godown = lambda g, d: (f"{g} - TC" if g else d)
        res, by_key = ImportResult("Stock Reconciliation"), {}
        for wh, q, r, v, b in imp._placements(item, default):
            imp._add_opening_row(by_key, item["_name"], wh, q, r, v,
                                 item.get("StandardCost"), res, batch=b)
        return list(by_key.values()), res

    def test_negative_godown_allocation_falls_back_to_item_level(self):
        """+99 in one godown and -58 in another net to the item-level 41. ERPNext
        cannot hold the -58, so post the item-level net (41) at the item rate - never
        the gross 99, and never at zero value."""
        item = {"_name": "Case", "IsBatchWiseOn": "No", "OpeningBalance": "41 PCS",
                "OpeningRate": "758.48/PCS", "OpeningValue": "", "StandardCost": "",
                "GodownOpenings": [
                    {"godown": "CHD", "qty": "99 PCS", "rate": "", "value": ""},
                    {"godown": "Main", "qty": "-58 PCS", "rate": "", "value": ""}]}
        rows, _ = self._godown_rows(item)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["qty"], 41.0)
        self.assertEqual(rows[0]["valuation_rate"], 758.48)
        self.assertEqual(rows[0]["warehouse"], "WH - TC")   # item-level default

    def test_divergent_per_godown_rate_uses_item_rate(self):
        """Godowns carrying their own (higher) rate are still valued at the item-level
        rate, so the posted total ties to item_qty x item_rate, not the godown rates."""
        item = {"_name": "Pods", "IsBatchWiseOn": "No", "OpeningBalance": "2 Nos",
                "OpeningRate": "1000.00/Nos", "OpeningValue": "", "StandardCost": "",
                "GodownOpenings": [
                    {"godown": "A", "qty": "1 Nos", "rate": "1500.00/Nos", "value": "-1500.00"},
                    {"godown": "B", "qty": "1 Nos", "rate": "1500.00/Nos", "value": "-1500.00"}]}
        rows, _ = self._godown_rows(item)
        self.assertEqual(sum(r["qty"] for r in rows), 2.0)
        self.assertTrue(all(r["valuation_rate"] == 1000.0 for r in rows))

    def test_godown_qty_mismatch_falls_back_to_item_level(self):
        """When the godown allocations do not sum to the item-level quantity the split
        is not faithful, so post the item-level row (which always reconciles)."""
        item = {"_name": "Widget", "IsBatchWiseOn": "No", "OpeningBalance": "30 Nos",
                "OpeningRate": "10.00/Nos", "OpeningValue": "", "StandardCost": "",
                "GodownOpenings": [
                    {"godown": "A", "qty": "15 Nos", "rate": "", "value": ""},
                    {"godown": "B", "qty": "10 Nos", "rate": "", "value": ""}]}   # sums to 25
        rows, _ = self._godown_rows(item)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["qty"], 30.0)
        self.assertEqual(rows[0]["valuation_rate"], 10.0)
        self.assertEqual(rows[0]["warehouse"], "WH - TC")

    def test_clean_godown_split_posts_per_warehouse_at_item_rate(self):
        """A clean, all-positive split that ties to the item quantity posts one row per
        warehouse, each at the item-level rate (total == item_qty x item_rate)."""
        item = {"_name": "Cable", "IsBatchWiseOn": "No", "OpeningBalance": "30 Nos",
                "OpeningRate": "10.00/Nos", "OpeningValue": "", "StandardCost": "",
                "GodownOpenings": [
                    {"godown": "A", "qty": "20 Nos", "rate": "", "value": ""},
                    {"godown": "B", "qty": "10 Nos", "rate": "", "value": ""}]}
        rows, _ = self._godown_rows(item)
        by_wh = {r["warehouse"]: r for r in rows}
        self.assertEqual(by_wh["A - TC"]["qty"], 20.0)
        self.assertEqual(by_wh["B - TC"]["qty"], 10.0)
        self.assertTrue(all(r["valuation_rate"] == 10.0 for r in rows))
        self.assertEqual(sum(r["qty"] * r["valuation_rate"] for r in rows), 300.0)

    def test_item_opening_qty_and_rate_helpers(self):
        """The shared qty/rate rule: item level wins, else the godown allocations."""
        from tally_migrator.tally.extractors import TallyExtractor as TE
        # qty: item-level OpeningBalance, else summed godown qty
        self.assertEqual(TE.item_opening_qty({"OpeningBalance": "41 PCS"}), 41.0)
        self.assertEqual(TE.item_opening_qty(
            {"OpeningBalance": "", "GodownOpenings": [{"qty": "20 Nos"}, {"qty": "10 Nos"}]}),
            30.0)
        # rate: item rate, then value/qty, then summed godown value/qty
        self.assertEqual(TE.item_opening_rate(
            {"OpeningBalance": "10 Nos", "OpeningRate": "5.00/Nos"}), 5.0)
        self.assertEqual(TE.item_opening_rate(
            {"OpeningBalance": "10 Nos", "OpeningValue": "-250.00"}), 25.0)
        self.assertEqual(TE.item_opening_rate(
            {"OpeningBalance": "30 Nos", "GodownOpenings": [
                {"value": "-200.00"}, {"value": "-100.00"}]}), 10.0)

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


class TestPartyPanSanitisation(unittest.TestCase):
    """Tally's INCOMETAXNumber lands in the party PAN field, but India Compliance
    validates PAN as ^[A-Z]{5}[0-9]{4}[A-Z]$ and rejects the whole party on a
    mismatch. Real books carry a TAN, a typo, or plain digits there (e.g.
    '4870030501'), which previously failed creation and lost the party. The
    importer must keep a valid PAN and silently blank an invalid one, so the party
    always imports - same fallback as an invalid GSTIN."""

    def _imp(self):
        from tally_migrator.erpnext.importers import SupplierImporter
        return SupplierImporter("_TMTest Co", "TC")

    def test_valid_pan_kept(self):
        self.assertEqual(self._imp()._valid_pan("AAACT2727Q"), "AAACT2727Q")

    def test_valid_pan_normalised(self):
        # lowercase + surrounding whitespace still recognised
        self.assertEqual(self._imp()._valid_pan("  aaact2727q  "), "AAACT2727Q")

    def test_invalid_pan_blanked(self):
        # the Brightpoint case: 10 digits, no letters -> not a PAN
        self.assertEqual(self._imp()._valid_pan("4870030501"), "")

    def test_partial_or_garbage_blanked(self):
        for bad in ("AAACT2727", "AAACT2727QQ", "12345ABCDZ", "ABCDE12345", "", None):
            self.assertEqual(self._imp()._valid_pan(bad), "",
                             msg="should reject %r" % (bad,))

    def test_build_doc_drops_invalid_pan_but_keeps_party(self):
        imp = self._imp()
        doc = imp.build_doc({"_name": "_TMTest Brightpoint",
                             "INCOMETAXNumber": "4870030501"})
        self.assertEqual(doc["supplier_name"], "_TMTest Brightpoint")
        self.assertEqual(doc["pan"], "")     # bad PAN dropped, party still builds

    def test_build_doc_keeps_valid_pan(self):
        imp = self._imp()
        doc = imp.build_doc({"_name": "_TMTest Good",
                             "INCOMETAXNumber": "AAACT2727Q"})
        self.assertEqual(doc["pan"], "AAACT2727Q")
