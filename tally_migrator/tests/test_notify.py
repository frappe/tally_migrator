"""Run-finished desk notification (master_migrator.notify_run_finished).

A background (async) run may finish after the wizard tab is closed, so the initiating
user is told via a persistent Notification Log entry. A synchronous run (no job_id)
returns its result to the open wizard, so it must stay silent. These lock the gating
without inserting a real Notification Log (frappe.get_doc is stubbed)."""
import unittest
from unittest import mock

from tally_migrator.migration import master_migrator as mm


class _Log:
    def __init__(self, **d):
        self._d = d
        self.name = d.get("name", "TML-1")

    def get(self, k):
        return self._d.get(k)


class TestRunFinishedNotification(unittest.TestCase):
    def _capture(self, log):
        """Return the list of docdicts notify_run_finished tried to insert."""
        created = []

        def fake_get_doc(doc):
            created.append(doc)
            return mock.Mock()   # .insert(ignore_permissions=True) is a no-op

        with mock.patch.object(mm.frappe, "get_doc", side_effect=fake_get_doc), \
                mock.patch.object(mm.frappe, "log_error"):
            mm.notify_run_finished(log)
        return created

    def test_async_completed_notifies_owner_with_link(self):
        created = self._capture(
            _Log(name="L9", job_id="j1", owner="ann@example.com", status="Completed"))
        self.assertEqual(len(created), 1)
        doc = created[0]
        self.assertEqual(doc["doctype"], "Notification Log")
        self.assertEqual(doc["for_user"], "ann@example.com")
        self.assertEqual(doc["document_type"], "Tally Migration Log")
        self.assertEqual(doc["document_name"], "L9")
        self.assertIn("successfully", doc["subject"])

    def test_completed_with_errors_and_failed_notify(self):
        for status, needle in (("Completed with Errors", "attention"),
                               ("Failed", "could not be completed")):
            created = self._capture(
                _Log(job_id="j", owner="ann@example.com", status=status))
            self.assertEqual(len(created), 1, status)
            self.assertIn(needle, created[0]["subject"])

    def test_sync_run_without_job_id_is_silent(self):
        self.assertEqual(
            self._capture(_Log(job_id="", owner="ann@example.com", status="Completed")), [])

    def test_administrator_and_guest_owner_skipped(self):
        for owner in ("Administrator", "Guest", "", None):
            self.assertEqual(
                self._capture(_Log(job_id="j", owner=owner, status="Completed")), [])

    def test_non_terminal_status_skipped(self):
        self.assertEqual(
            self._capture(_Log(job_id="j", owner="ann@example.com", status="Running")), [])


if __name__ == "__main__":
    unittest.main()
