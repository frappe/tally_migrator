"""Tests for the optional migration-rollback feature.

The deletion ordering and manifest parsing are pure and tested directly; the
endpoint's guard rails (feature flag, company confirmation, already-reverted) are
tested with light mocking. The full live-DB delete happy-path is exercised by the
integration suite, which has a real company to create and tear down.
"""
import unittest
from unittest import mock

import frappe

from tally_migrator.migration import rollback


class TestDeletionOrder(unittest.TestCase):
    """_deletion_order flattens the manifest into reverse-creation order."""

    def test_reverses_pipeline_order(self):
        created = {
            "Accounts": [{"name": "Cash", "doctype": "Account"}],
            "Items": [{"name": "Widget", "doctype": "Item"}],
            "Opening Stock": [{"name": "SR-1", "doctype": "Stock Reconciliation"}],
        }
        rows = rollback._deletion_order(created)
        order = [r["doctype"] for r in rows]
        # Stock Reconciliation (last created) must be deleted before Account (first).
        self.assertEqual(order, ["Stock Reconciliation", "Item", "Account"])

    def test_order_ignores_alphabetised_manifest_keys(self):
        # The manifest is stored via as_json, which sorts keys alphabetically, so
        # "Suppliers" sorts before "Party Openings". Deletion must still remove the
        # Party Openings (which reference a supplier's address) FIRST, by creation
        # rank - not by the manifest's key order.
        created = {
            "Party Openings": [{"name": "PINV-1", "doctype": "Purchase Invoice"}],
            "Suppliers": [{"name": "ACME", "doctype": "Supplier"}],
        }
        order = [r["doctype"] for r in rollback._deletion_order(created)]
        self.assertEqual(order, ["Purchase Invoice", "Supplier"])

    def test_unknown_label_is_deleted_first(self):
        created = {
            "Accounts": [{"name": "Cash", "doctype": "Account"}],
            "Mystery": [{"name": "X", "doctype": "Item"}],
        }
        order = [r["doctype"] for r in rollback._deletion_order(created)]
        self.assertEqual(order, ["Item", "Account"])

    def test_resolves_legacy_bare_string_via_label(self):
        created = {"Accounts": ["Cash", "Bank"]}
        rows = rollback._deletion_order(created)
        self.assertTrue(all(r["doctype"] == "Account" for r in rows))
        # Within a label, also reversed.
        self.assertEqual([r["name"] for r in rows], ["Bank", "Cash"])

    def test_skips_entries_with_no_resolvable_doctype(self):
        created = {"Mystery Label": ["x"]}  # unknown label, bare string -> no doctype
        self.assertEqual(rollback._deletion_order(created), [])

    def test_prefers_item_doctype_over_label_map(self):
        created = {"Suppliers": [{"name": "ACME-Bank", "doctype": "Bank Account"}]}
        rows = rollback._deletion_order(created)
        self.assertEqual(rows[0]["doctype"], "Bank Account")


class TestFeatureFlag(unittest.TestCase):
    def test_is_enabled_reads_site_config(self):
        with mock.patch.object(rollback.frappe, "conf", {"tally_migrator_enable_rollback": 1}):
            self.assertTrue(rollback.is_enabled())
        with mock.patch.object(rollback.frappe, "conf", {}):
            self.assertFalse(rollback.is_enabled())


class TestRevertGuards(unittest.TestCase):
    """revert_migration must refuse before deleting anything."""

    def setUp(self):
        # Neutralise the role check for the unit tests.
        self._only_for = mock.patch.object(rollback.frappe, "only_for", lambda *a, **k: None)
        self._only_for.start()
        self.addCleanup(self._only_for.stop)

    def test_disabled_flag_raises_permission_error(self):
        with mock.patch.object(rollback, "is_enabled", return_value=False):
            with self.assertRaises(frappe.PermissionError):
                rollback.revert_migration("TML-0001", "Frappe Tech")

    def test_company_mismatch_aborts(self):
        log = mock.Mock(company="Frappe Tech", status="Completed", created_records="{}")
        with mock.patch.object(rollback, "is_enabled", return_value=True), \
             mock.patch.object(rollback.frappe, "get_doc", return_value=log):
            with self.assertRaises(frappe.exceptions.ValidationError):
                rollback.revert_migration("TML-0001", "Wrong Co")

    def test_already_reverted_aborts(self):
        log = mock.Mock(company="Frappe Tech", status="Reverted", created_records="{}")
        with mock.patch.object(rollback, "is_enabled", return_value=True), \
             mock.patch.object(rollback.frappe, "get_doc", return_value=log):
            with self.assertRaises(frappe.exceptions.ValidationError):
                rollback.revert_migration("TML-0001", "Frappe Tech")


if __name__ == "__main__":
    unittest.main()
