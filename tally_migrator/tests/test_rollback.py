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

    def test_prices_and_boms_deleted_before_items(self):
        # Item Price / Pricing Rule / BOM reference the Item, so they must be removed
        # before it (else the Item delete is blocked / would dangle).
        created = {
            "Items": [{"name": "Widget", "doctype": "Item"}],
            "Prices": [{"name": "IP-1", "doctype": "Item Price"},
                       {"name": "PRLE-0001", "doctype": "Pricing Rule"}],
            "BOMs": [{"name": "BOM-Widget-001", "doctype": "BOM"}],
        }
        order = [r["doctype"] for r in rollback._deletion_order(created)]
        self.assertLess(order.index("BOM"), order.index("Item"))
        self.assertLess(order.index("Item Price"), order.index("Item"))
        self.assertLess(order.index("Pricing Rule"), order.index("Item"))

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


class TestDeleteOneSafety(unittest.TestCase):
    """_delete_one must NOT force past the link check: a master still referenced by
    post-migration activity has to be KEPT (reported), never silently deleted."""

    def _patches(self, delete_side_effect=None):
        # Minimal frappe surface for _delete_one: a non-submitted master that exists.
        calls = {}

        def fake_delete_doc(doctype, name, **kw):
            calls["delete_kwargs"] = kw
            if delete_side_effect:
                raise delete_side_effect

        return calls, [
            mock.patch.object(rollback.frappe.db, "exists", return_value=True),
            mock.patch.object(rollback.frappe.db, "savepoint", lambda *a, **k: None),
            mock.patch.object(rollback.frappe.db, "rollback", lambda *a, **k: None),
            mock.patch.object(rollback.frappe, "get_doc",
                              return_value=mock.Mock(docstatus=0)),
            mock.patch.object(rollback.frappe, "delete_doc", side_effect=fake_delete_doc),
            mock.patch.object(rollback.frappe, "local", mock.Mock(message_log=[])),
        ]

    def test_delete_never_forces_past_link_check(self):
        calls, patches = self._patches()
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        reason = rollback._delete_one({"doctype": "Customer", "name": "ACME"}, set())
        self.assertIsNone(reason)                              # clean delete
        # The crux: force must not be passed (default False keeps the link check that
        # protects Item/Customer/Supplier/Cost Center, whose on_trash does not guard).
        self.assertNotIn("force", calls["delete_kwargs"])

    def test_linked_record_is_kept_not_deleted(self):
        link_err = frappe.LinkExistsError("Linked with Sales Invoice SI-9 (post-migration)")
        calls, patches = self._patches(delete_side_effect=link_err)
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        reason = rollback._delete_one({"doctype": "Item", "name": "Widget"}, set())
        self.assertIn("SI-9", reason)                          # kept + reported, not deleted


class TestRevertGuards(unittest.TestCase):
    """revert_migration must refuse before deleting anything."""

    def setUp(self):
        # Neutralise the role check for the unit tests.
        self._only_for = mock.patch.object(rollback.frappe, "only_for", lambda *a, **k: None)
        self._only_for.start()
        self.addCleanup(self._only_for.stop)

    def test_company_mismatch_aborts(self):
        log = mock.Mock(company="Frappe Tech", status="Completed", created_records="{}")
        with mock.patch.object(rollback.frappe, "get_doc", return_value=log):
            with self.assertRaises(frappe.exceptions.ValidationError):
                rollback.revert_migration("TML-0001", "Wrong Co")

    def test_already_reverted_aborts(self):
        log = mock.Mock(company="Frappe Tech", status="Reverted", created_records="{}")
        with mock.patch.object(rollback.frappe, "get_doc", return_value=log):
            with self.assertRaises(frappe.exceptions.ValidationError):
                rollback.revert_migration("TML-0001", "Frappe Tech")


if __name__ == "__main__":
    unittest.main()
