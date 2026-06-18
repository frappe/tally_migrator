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


class TestSideEffects(unittest.TestCase):
    def test_uom_category_purges_its_conversion_factors(self):
        # The auto-created "Tally Imported" UOM Category is blocked by UOM Conversion
        # Factors naming it; deleting the category must purge those first so it goes
        # regardless of UOM deletion order.
        self.assertIn(("UOM Conversion Factor", "category"),
                      rollback._SIDE_EFFECTS["UOM Category"])


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
            # Neutralise the pre-delete steps so the test isolates the link-check:
            # no side-effect tables, no party links to purge.
            mock.patch.object(rollback.frappe.db, "table_exists", return_value=False),
            mock.patch.object(rollback.frappe.db, "delete", lambda *a, **k: None),
            mock.patch.object(rollback.frappe.db, "set_value", lambda *a, **k: None),
            mock.patch.object(rollback.frappe, "get_all", return_value=[]),
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

    def test_recon_bundle_force_deleted_before_voucher(self):
        # The opening Stock Reconciliation auto-creates a Serial and Batch Bundle per
        # batch row. Cancelling the recon only delinks + flags it; the submitted
        # Bundle would block the recon's unforced delete. _delete_one must force-delete
        # the Bundle (found by voucher_no) BEFORE deleting the reconciliation.
        deleted = []

        def fake_delete_doc(doctype, name, **kw):
            deleted.append((doctype, name, kw.get("force")))

        patches = [
            mock.patch.object(rollback.frappe.db, "exists", return_value=True),
            mock.patch.object(rollback.frappe.db, "savepoint", lambda *a, **k: None),
            mock.patch.object(rollback.frappe.db, "rollback", lambda *a, **k: None),
            mock.patch.object(rollback.frappe.db, "delete", lambda *a, **k: None),
            mock.patch.object(rollback.frappe.db, "table_exists", return_value=True),
            mock.patch.object(rollback.frappe, "get_all",
                              side_effect=lambda dt, **k: ["SBB-1"]
                              if dt == "Serial and Batch Bundle" else []),
            mock.patch.object(rollback.frappe, "get_doc",
                              return_value=mock.Mock(docstatus=1)),
            mock.patch.object(rollback.frappe, "delete_doc", side_effect=fake_delete_doc),
            mock.patch.object(rollback.frappe, "local", mock.Mock(message_log=[])),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        reason = rollback._delete_one(
            {"doctype": "Stock Reconciliation", "name": "SR-1"}, set())
        self.assertIsNone(reason)
        # Bundle force-deleted, and ordered before the reconciliation itself.
        self.assertEqual(deleted[0], ("Serial and Batch Bundle", "SBB-1", True))
        self.assertIn(("Stock Reconciliation", "SR-1", None), deleted)
        self.assertLess(deleted.index(("Serial and Batch Bundle", "SBB-1", True)),
                        deleted.index(("Stock Reconciliation", "SR-1", None)))


class TestPurgePartyLinks(unittest.TestCase):
    """_purge_party_links must never strip a record shared with another party, and
    must report the business records it does delete so the audit can list them."""

    def _run(self, dynamic_links, link_rows_by_parent, bank_accounts):
        """Drive _purge_party_links against a fake Dynamic Link / Bank Account store.

        ``dynamic_links`` -> the parents linked to OUR party per linked_dt query;
        ``link_rows_by_parent`` -> every link row on a given parent (to detect sharing);
        ``bank_accounts`` -> names returned for the Bank Account query.
        """
        deleted, dl_deletes, cleared = [], [], []

        def fake_get_all(doctype, filters=None, pluck=None, fields=None):
            if doctype == "Dynamic Link" and "link_name" in (filters or {}):
                return list(dynamic_links.get(filters["parenttype"], []))
            if doctype == "Dynamic Link":  # all links on a parent (share check)
                return [frappe._dict(r) for r in link_rows_by_parent.get(filters["parent"], [])]
            if doctype == "Bank Account":
                return list(bank_accounts)
            return []

        def fake_db_delete(doctype, filters):
            dl_deletes.append((doctype, filters))

        patches = [
            mock.patch.object(rollback.frappe, "get_all", side_effect=fake_get_all),
            mock.patch.object(rollback.frappe.db, "exists", return_value=True),
            mock.patch.object(rollback.frappe.db, "delete", side_effect=fake_db_delete),
            mock.patch.object(rollback.frappe.db, "set_value",
                              side_effect=lambda dt, nm, f, v: cleared.append((dt, nm, f, v))),
            mock.patch.object(rollback.frappe, "delete_doc",
                              side_effect=lambda dt, nm, **k: deleted.append((dt, nm))),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        purged = rollback._purge_party_links("Customer", "ACME")
        self._cleared = cleared
        return purged, deleted, dl_deletes

    def test_unshared_records_deleted_and_reported(self):
        purged, deleted, dl_deletes = self._run(
            dynamic_links={"Address": ["ADDR-1"], "Contact": ["CON-1"]},
            link_rows_by_parent={
                "ADDR-1": [{"link_doctype": "Customer", "link_name": "ACME"}],
                "CON-1": [{"link_doctype": "Customer", "link_name": "ACME"}],
            },
            bank_accounts=["ACME - HDFC"],
        )
        self.assertIn(("Address", "ADDR-1"), deleted)
        self.assertIn(("Contact", "CON-1"), deleted)
        self.assertIn(("Bank Account", "ACME - HDFC"), deleted)
        self.assertEqual(dl_deletes, [])  # nothing shared -> no link-row surgery
        # All three reported so the revert audit can list them.
        self.assertEqual(len(purged), 3)

    def test_shared_address_is_not_deleted_only_unlinked(self):
        purged, deleted, dl_deletes = self._run(
            dynamic_links={"Address": ["SHARED"], "Contact": []},
            link_rows_by_parent={
                "SHARED": [
                    {"link_doctype": "Customer", "link_name": "ACME"},
                    {"link_doctype": "Customer", "link_name": "OTHER"},  # another party!
                ],
            },
            bank_accounts=[],
        )
        # The shared address must survive; only OUR link row is removed.
        self.assertNotIn(("Address", "SHARED"), deleted)
        self.assertEqual(len(dl_deletes), 1)
        self.assertEqual(dl_deletes[0][1]["link_name"], "ACME")
        self.assertEqual(purged, [])  # nothing actually deleted -> nothing to report

    def test_primary_links_cleared_before_deleting_records(self):
        # The party's own customer_primary_address/_contact point at the Address/
        # Contact, so deleting those is refused until the links are cleared. They
        # must be nulled (one set_value per primary field) so the records - and then
        # the party - can be removed.
        self._run(
            dynamic_links={"Address": ["ADDR-1"], "Contact": ["CON-1"]},
            link_rows_by_parent={
                "ADDR-1": [{"link_doctype": "Customer", "link_name": "ACME"}],
                "CON-1": [{"link_doctype": "Customer", "link_name": "ACME"}],
            },
            bank_accounts=[],
        )
        self.assertIn(("Customer", "ACME", "customer_primary_address", None), self._cleared)
        self.assertIn(("Customer", "ACME", "customer_primary_contact", None), self._cleared)


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

    @staticmethod
    def _log_get_doc(log):
        """get_doc that returns our fake log for the migration log, but delegates
        everything else (e.g. the internal System Settings read in now_datetime) to
        the real implementation - patching it globally would cache a Mock and crash."""
        real = rollback.frappe.get_doc

        def _gd(doctype, *a, **k):
            return log if doctype == "Tally Migration Log" else real(doctype, *a, **k)

        return _gd

    def test_reverted_with_errors_is_rerunnable(self):
        # A partial revert ("Reverted with Errors") must NOT be treated as already
        # reverted - the user has to be able to retry after fixing the cause.
        log = mock.Mock(company="Frappe Tech", status="Reverted with Errors",
                        name="TML-1", created_records='{"Items": [{"name": "W", "doctype": "Item"}]}')
        new_revert = mock.Mock()
        with mock.patch.object(rollback.frappe, "get_doc", side_effect=self._log_get_doc(log)), \
             mock.patch.object(rollback.frappe, "get_all", return_value=[]), \
             mock.patch.object(rollback.frappe, "new_doc", return_value=new_revert), \
             mock.patch.object(rollback.frappe.db, "commit", lambda *a, **k: None), \
             mock.patch.object(rollback.frappe, "enqueue", lambda *a, **k: None):
            rollback.revert_migration("TML-1", "Frappe Tech")
            self.assertTrue(new_revert.insert.called)   # proceeded to queue a revert

    def test_stale_in_flight_is_superseded(self):
        # An In Progress revert older than the job timeout is a corpse: mark it Failed
        # and allow a new one, so a killed worker never wedges the feature forever.
        log = mock.Mock(company="Frappe Tech", status="Reverted with Errors",
                        name="TML-1", created_records='{"Items": [{"name": "W", "doctype": "Item"}]}')
        stale = frappe._dict(name="REV-OLD", modified="2000-01-01 00:00:00")
        set_calls = {}
        with mock.patch.object(rollback.frappe, "get_doc", side_effect=self._log_get_doc(log)), \
             mock.patch.object(rollback.frappe, "get_all", return_value=[stale]), \
             mock.patch.object(rollback.frappe, "new_doc", return_value=mock.Mock()), \
             mock.patch.object(rollback.frappe.db, "commit", lambda *a, **k: None), \
             mock.patch.object(rollback.frappe, "enqueue", lambda *a, **k: None), \
             mock.patch.object(rollback.frappe.db, "set_value",
                               side_effect=lambda dt, nm, vals: set_calls.update({nm: vals})):
            rollback.revert_migration("TML-1", "Frappe Tech")
        # The corpse was flagged Failed rather than blocking the new revert.
        self.assertEqual(set_calls.get("REV-OLD", {}).get("status"), "Failed")


if __name__ == "__main__":
    unittest.main()
