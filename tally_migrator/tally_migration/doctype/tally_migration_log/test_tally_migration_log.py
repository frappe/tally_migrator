import unittest

import frappe

from tally_migrator.tests.utils import require_company


class TestTallyMigrationLog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        frappe.set_user("Administrator")
        cls.company = require_company()  # skips the whole class when no Company exists

    def tearDown(self):
        frappe.db.rollback()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _make_log(self, **kwargs) -> "frappe.Document":
        defaults = {
            "doctype":        "Tally Migration Log",
            "company":        self.company,
            "tally_company":  "Test Company",
            "migration_type": "Masters",
            "status":         "Running",
        }
        doc = frappe.get_doc({**defaults, **kwargs})
        doc.insert(ignore_permissions=True)
        return doc

    # ── Tests ─────────────────────────────────────────────────────────────────

    def test_create_log(self):
        doc = self._make_log()
        self.assertTrue(doc.name.startswith("TML-"))

    def test_autoname_year_prefix(self):
        import datetime
        doc = self._make_log()
        year = str(datetime.datetime.now().year)
        self.assertIn(year, doc.name)

    def test_migration_date_set_on_insert(self):
        doc = self._make_log()
        self.assertIsNotNone(doc.migration_date)

    def test_status_defaults_to_running(self):
        doc = self._make_log()
        self.assertEqual(doc.status, "Running")

    def test_update_status_to_completed(self):
        doc = self._make_log()
        doc.status = "Completed"
        doc.save(ignore_permissions=True)
        reloaded = frappe.get_doc("Tally Migration Log", doc.name)
        self.assertEqual(reloaded.status, "Completed")

    def test_import_summary_stores_json(self):
        import json
        summary = {"Customers": {"created": 10, "skipped": 2, "failed": 0, "errors": []}}
        doc = self._make_log(import_summary=json.dumps(summary))
        doc.reload()
        parsed = json.loads(doc.import_summary)
        self.assertEqual(parsed["Customers"]["created"], 10)

    def test_permissions_deny_guest(self):
        # A real permissioned insert (not the ignore_permissions helper, which would
        # bypass the very check under test). No role is granted create on this
        # doctype - logs are created server-side during a migration run - so a Guest
        # insert must be denied.
        frappe.set_user("Guest")
        try:
            doc = frappe.get_doc({
                "doctype":        "Tally Migration Log",
                "company":        self.company,
                "tally_company":  "Test Company",
                "migration_type": "Masters",
                "status":         "Running",
            })
            with self.assertRaises(frappe.PermissionError):
                doc.insert()
        finally:
            frappe.set_user("Administrator")
