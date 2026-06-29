"""Guard: _finalize_log must always leave the log in a terminal state, even when the
heavy enrichment (manifest / reconciliation / per-record errors) blows up. This is the
bug that froze the wizard on large async runs - status stayed 'Running' forever while
run() still published 100%, so the page sat at a full bar with no results."""
import unittest
from types import SimpleNamespace
from unittest import mock

from tally_migrator.migration.master_migrator import MasterMigrator


class _FakeLog:
    """A stand-in for the Tally Migration Log doc - records writes without a DB."""

    def __init__(self):
        self.status = "Running"
        self.import_summary = None
        self.created_records = None
        self.reconciliation_report = None
        self._saves = 0

    def reload(self):
        pass

    def set(self, field, value):
        setattr(self, field, value)

    def append(self, *a, **k):
        pass

    def db_set(self, field, value, commit=False):
        setattr(self, field, value)

    def save(self, **k):
        self._saves += 1


def _summary(has_errors=False, has_warnings=False):
    return SimpleNamespace(
        has_errors=has_errors,
        has_warnings=has_warnings,
        as_dict=lambda: {"Accounts": {"created": 1}},
        created_records=lambda: {"Account": [{"name": "X"}]},
        error_lines=lambda: "",
        error_records=lambda: [],
    )


class TestFinalizeTerminalStatus(unittest.TestCase):
    def _migrator(self, log):
        # Bypass __init__ - we only exercise _finalize_log against a fake self.
        m = MasterMigrator.__new__(MasterMigrator)
        m.log = log
        m.applied_edits = []
        m._timings = {}
        m.created_uoms = []
        return m

    def _masters_coa(self):
        return SimpleNamespace(summary={}), SimpleNamespace(summary={})

    @mock.patch("frappe.db")
    @mock.patch("frappe.log_error")
    def test_status_committed_when_enrichment_fails(self, _log_err, _db):
        log = _FakeLog()
        m = self._migrator(log)
        masters, coa = self._masters_coa()
        # Make the enrichment phase blow up at the manifest step.
        summary = _summary()
        summary.created_records = mock.Mock(side_effect=RuntimeError("boom"))
        m._finalize_log(masters, coa, summary)
        # Terminal state survived the enrichment failure.
        self.assertEqual(log.status, "Completed")
        self.assertIsNotNone(log.import_summary)

    @mock.patch("frappe.db")
    @mock.patch("frappe.log_error")
    def test_reports_persist_when_issues_table_fails(self, _log_err, _db):
        # A failure writing the issues table (2b) - e.g. a row-level length limit on a
        # big run - must not also lose the records-created / reconciliation reports (2a).
        log = _FakeLog()
        m = self._migrator(log)
        m._reconciliation = lambda *a: {"rows": []}
        m._track_wizard_uoms = lambda *a: None
        masters, coa = self._masters_coa()
        summary = _summary(has_errors=True)
        summary.error_records = mock.Mock(side_effect=RuntimeError("row too long"))
        m._finalize_log(masters, coa, summary)
        self.assertEqual(log.status, "Completed with Errors")
        self.assertIsNotNone(log.created_records)        # 2a survived
        self.assertIsNotNone(log.reconciliation_report)  # 2a survived

    @mock.patch("frappe.db")
    @mock.patch("frappe.log_error")
    def test_status_reflects_errors(self, _log_err, _db):
        log = _FakeLog()
        m = self._migrator(log)
        masters, coa = self._masters_coa()
        summary = _summary(has_errors=True)
        m._reconciliation = lambda *a: {}
        m._track_wizard_uoms = lambda *a: None
        m._finalize_log(masters, coa, summary)
        self.assertEqual(log.status, "Completed with Errors")

    @mock.patch("frappe.db")
    @mock.patch("frappe.log_error")
    def test_status_fallback_when_first_save_fails(self, _log_err, _db):
        log = _FakeLog()
        # First save() raises; the db_set fallback must still set a terminal status.
        original_save = log.save
        calls = {"n": 0}

        def flaky_save(**k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("save exploded")
            return original_save(**k)

        log.save = flaky_save
        m = self._migrator(log)
        m._reconciliation = lambda *a: {}
        m._track_wizard_uoms = lambda *a: None
        masters, coa = self._masters_coa()
        m._finalize_log(masters, coa, _summary())
        self.assertEqual(log.status, "Completed")


if __name__ == "__main__":
    unittest.main()
