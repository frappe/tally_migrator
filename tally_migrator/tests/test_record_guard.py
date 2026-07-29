"""Tests for the per-record hang guard (record_guard).

Covers: config + defaults, the Redis in-flight marker, the retry-once -> confirm state
machine, should_skip / guarded_records skip behaviour, guard arm/cancel + re-entrancy,
and - in a real subprocess - that a hung record is actually dumped and hard-killed with
its exact stack captured. Pure-logic parts need no site (mocked like test_async_decision);
the subprocess part is self-contained.
"""
import faulthandler
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from tally_migrator.migration import record_guard as rg


class _FakeLog:
    """Minimal stand-in for a Tally Migration Log: get()/db_set() over a dict, which is
    all the guard-state helpers touch."""
    def __init__(self, name="TML-TEST"):
        self.name = name
        self._d = {}

    def get(self, k):
        return self._d.get(k)

    def db_set(self, k, v, **kw):
        self._d[k] = v


class TestGuardConfig(unittest.TestCase):
    def _conf(self, **kw):
        return mock.patch.object(rg.frappe, "conf", kw)

    def test_timeout_default_60(self):
        with self._conf():
            self.assertEqual(rg.record_timeout_s(), 60)

    def test_timeout_override(self):
        with self._conf(tally_migrator_record_timeout=30):
            self.assertEqual(rg.record_timeout_s(), 30)

    def test_timeout_invalid_falls_back(self):
        with self._conf(tally_migrator_record_timeout=-5):
            self.assertEqual(rg.record_timeout_s(), 60)
        with self._conf(tally_migrator_record_timeout="nonsense"):
            self.assertEqual(rg.record_timeout_s(), 60)

    def test_max_resumes_default_and_override(self):
        with self._conf():
            self.assertEqual(rg.max_resumes(), 50)
        with self._conf(tally_migrator_max_resumes=10):
            self.assertEqual(rg.max_resumes(), 10)


class TestMarker(unittest.TestCase):
    def test_marker_round_trip_and_clear(self):
        rg.set_inflight("MK1", "Customer", "Acme")
        m = rg.read_inflight("MK1")
        self.assertEqual(m["phase"], "Customer")
        self.assertEqual(m["ident"], "Acme")
        # A wall-clock timestamp is stamped so the resume sweep can detect a dead worker
        # even when RQ still reports the job 'started' (no-fork case).
        self.assertIsInstance(m["ts"], float)
        rg.clear_inflight("MK1")
        self.assertIsNone(rg.read_inflight("MK1"))


class TestStateMachine(unittest.TestCase):
    def test_retry_once_then_confirm(self):
        log = _FakeLog()
        self.assertEqual(rg.confirmed_from_log(log), set())
        # first stall -> suspect (attempts 1), NOT confirmed: a one-off slow record is
        # given a second chance, so a good record is never dropped.
        attempts, resume = rg.note_stall(log, "Customer", "Acme")
        self.assertEqual((attempts, resume), (1, 1))
        self.assertEqual(rg.confirmed_from_log(log), set())
        # second stall -> confirmed (attempts 2) -> skip on sight
        attempts, resume = rg.note_stall(log, "Customer", "Acme")
        self.assertEqual((attempts, resume), (2, 2))
        self.assertEqual(rg.confirmed_from_log(log), {("Customer", "Acme")})

    def test_resume_count_is_shared_attempts_are_per_record(self):
        log = _FakeLog()
        rg.note_stall(log, "Customer", "A")
        attempts, resume = rg.note_stall(log, "Customer", "B")
        self.assertEqual(attempts, 1)     # B's own first stall
        self.assertEqual(resume, 2)       # but the second resume overall

    def test_state_survives_reload(self):
        log = _FakeLog()
        rg.note_stall(log, "Item", "Widget")
        rg.note_stall(log, "Item", "Widget")
        # a fresh log object reading the same persisted field sees the confirmation
        reloaded = _FakeLog()
        reloaded._d["guard_state"] = log._d["guard_state"]
        self.assertEqual(rg.confirmed_from_log(reloaded), {("Item", "Widget")})


class TestSkipAndGuard(unittest.TestCase):
    def tearDown(self):
        rg.deactivate()
        faulthandler.cancel_dump_traceback_later()

    def test_should_skip_noop_when_inactive(self):
        rg.deactivate()
        self.assertFalse(rg.should_skip("Customer", "Anything"))

    def test_should_skip_only_confirmed(self):
        rg.activate(rg.RecordGuard("L", confirmed={("Customer", "Acme")}))
        self.assertTrue(rg.should_skip("Customer", "Acme"))
        self.assertFalse(rg.should_skip("Customer", "Fresh"))

    def test_guarded_records_skips_confirmed_and_reports(self):
        rg.activate(rg.RecordGuard("L", confirmed={("Customer", "Acme")}, timeout_s=999))
        skipped, seen = [], []
        recs = [{"_name": "Acme"}, {"_name": "Fresh"}, {"_name": "Zed"}]
        for r in rg.guarded_records("Customer", recs, lambda x: x["_name"],
                                    on_skip=lambda r, i: skipped.append(i)):
            seen.append(r["_name"])
        self.assertEqual(seen, ["Fresh", "Zed"])   # confirmed one never yielded
        self.assertEqual(skipped, ["Acme"])

    def test_guard_arms_and_cancels(self):
        rg.activate(rg.RecordGuard("L", timeout_s=999))
        with rg.guard("Customer", "X"):
            self.assertTrue(rg.current()._armed)
        self.assertFalse(rg.current()._armed)

    def test_guard_reentrancy_keeps_outer_timer(self):
        rg.activate(rg.RecordGuard("L", timeout_s=999))
        with rg.guard("Customer", "X"):
            with rg.guard("Customer", "Y"):   # nested must not clobber the outer timer
                pass
            self.assertTrue(rg.current()._armed)

    def test_guard_is_noop_when_inactive(self):
        rg.deactivate()
        with rg.guard("Customer", "X"):   # must not raise, must not arm
            pass
        self.assertIsNone(rg.current())


class TestActualKill(unittest.TestCase):
    """The whole point: a hung record must dump its stack and hard-kill the worker."""

    def test_hung_record_is_dumped_and_killed(self):
        dump_dir = tempfile.mkdtemp(prefix="tm_guard_kill_")
        child = textwrap.dedent(f"""
            import sys, types, json, time, os
            _c = {{}}
            class C:
                def set_value(s,k,v,expires_in_sec=None): _c[k]=v
                def get_value(s,k): return _c.get(k)
                def delete_value(s,k): _c.pop(k,None)
            f = types.ModuleType('frappe'); f.conf={{}}; f.cache=lambda: C()
            f.as_json=json.dumps; f.parse_json=lambda s: json.loads(s) if isinstance(s,str) else s
            f.get_site_path=lambda *a: os.path.join({dump_dir!r}, *a)
            sys.modules['frappe']=f
            sys.path.insert(0, {os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))!r})
            from tally_migrator.migration import record_guard as rg
            rg.activate(rg.RecordGuard('LKILL', timeout_s=1))
            def the_stuck_record():
                time.sleep(30)
            with rg.guard('Customer', 'StuckCo'):
                the_stuck_record()
            print('SHOULD_NOT_REACH')
        """)
        proc = subprocess.run([sys.executable, "-c", child], capture_output=True,
                              text=True, timeout=30)
        # hard-killed, not a normal exit
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("SHOULD_NOT_REACH", proc.stdout)
        dump = os.path.join(dump_dir, "tally_migrator_hangs", "LKILL.log")
        self.assertTrue(os.path.exists(dump), "hang dump file not written")
        content = open(dump).read()
        self.assertIn("Timeout", content)                 # faulthandler fired
        self.assertIn("the_stuck_record", content)         # exact hung line captured
        # (the record identity lives in the Redis marker + resume log, not the dump)


from tally_migrator.erpnext.importers.base import BaseImporter


class _FakeImporter(BaseImporter):
    """Exercises BaseImporter.run's guard wiring without touching the DB."""
    doctype = "GuardTestDT"
    key_field = "name"

    def __init__(self):
        super().__init__("Co", "C")
        self.processed = []

    def _prefetch_existing(self):
        return None

    def build_doc(self, record):
        return dict(record)

    def _upsert(self, result, data):
        self.processed.append(data["name"])
        result.add_created(data["name"])
        return data["name"], True


class TestBaseRunWiring(unittest.TestCase):
    def tearDown(self):
        rg.deactivate()
        faulthandler.cancel_dump_traceback_later()

    def test_noop_without_active_guard(self):
        rg.deactivate()
        imp = _FakeImporter()
        result = imp.run([{"name": "A"}, {"name": "B"}])
        self.assertEqual(imp.processed, ["A", "B"])
        self.assertEqual(result.created, 2)
        self.assertEqual(result.failed, 0)

    def test_confirmed_record_is_skipped_and_recorded_rest_import(self):
        imp = _FakeImporter()
        rg.activate(rg.RecordGuard("L", confirmed={("GuardTestDT", "B")}, timeout_s=999))
        result = imp.run([{"name": "A"}, {"name": "B"}, {"name": "C"}])
        self.assertEqual(imp.processed, ["A", "C"])          # B never processed
        self.assertEqual(result.created, 2)
        self.assertEqual(result.failed, 1)                   # B is a visible failure
        self.assertIn("B", [e["name"] for e in result.errors])


class _RealShapedImporter(BaseImporter):
    """Importer whose ``key_field`` is an ERPNext column (like the real Customer /
    Item importers), fed records shaped like ``FileTallySource.get_collection`` output
    (``_name`` + Tally field names, no lowercase ``name``, no ``key_field``)."""
    doctype = "Customer"
    key_field = "customer_name"     # ERPNext column - absent from a source record

    def __init__(self):
        super().__init__("Co", "C")
        self.processed = []

    def _prefetch_existing(self):
        return None

    def build_doc(self, record):
        return {"customer_name": record["_name"]}

    def _upsert(self, result, data):
        self.processed.append(data["customer_name"])
        result.add_created(data["customer_name"])
        return data["customer_name"], True


class TestRecordIdentity(unittest.TestCase):
    """Regression: the hang guard must identify each record by its Tally ``_name``.

    A record from ``get_collection`` is ``{"_name": <tally name>, "Name": ..., ...}`` -
    there is no lowercase ``name`` key, and ``key_field`` is the ERPNext column, which the
    source dict never carries. If ``_record_ident`` misses ``_name`` it returns "" for
    every record, so a single confirmed hang collapses onto the ``(doctype, "")`` key and
    skips the WHOLE phase on resume. These lock the identity to ``_name``.
    """

    def tearDown(self):
        rg.deactivate()

    def _rec(self, name):
        # Mirrors FileTallySource.get_collection: _name + Tally field names only.
        return {"_name": name, "Name": name, "Parent": "Sundry Debtors",
                "OpeningBalance": "-100"}

    def test_ident_is_the_tally_name_not_empty(self):
        imp = _RealShapedImporter()
        self.assertEqual(imp._record_ident(self._rec("Dhatu Industries")),
                         "Dhatu Industries")

    def test_confirmed_hang_skips_only_that_record_not_the_whole_phase(self):
        imp = _RealShapedImporter()
        # Only "Beta" is confirmed-hung; "Alpha"/"Gamma" must still import.
        rg.activate(rg.RecordGuard(
            "L", confirmed={("Customer", "Beta")}, timeout_s=999))
        result = imp.run([self._rec("Alpha"), self._rec("Beta"), self._rec("Gamma")])
        self.assertEqual(imp.processed, ["Alpha", "Gamma"])   # phase NOT wiped out
        self.assertEqual(result.created, 2)
        self.assertEqual(result.failed, 1)                    # only Beta failed
        self.assertIn("Beta", [e["name"] for e in result.errors])


class TestResumeSweep(unittest.TestCase):
    def _row(self):
        return {"name": "TML-R", "job_id": "j", "company": "Co"}

    def _log(self):
        log = _FakeLog("TML-R")
        log._d.update(source_file="/files/x.xml", company="Co")
        return log

    def test_dead_run_with_marker_reenqueues_and_records_stall(self):
        from tally_migrator.migration import resume
        log = self._log()
        with mock.patch.object(resume, "_job_alive", return_value=False), \
             mock.patch.object(resume.record_guard, "read_inflight",
                               return_value={"phase": "Customer", "ident": "Acme"}), \
             mock.patch.object(resume.frappe, "get_doc", return_value=log), \
             mock.patch.object(resume.frappe.db, "commit"), \
             mock.patch.object(resume, "_reenqueue") as reenq:
            resume._maybe_resume(self._row())
        reenq.assert_called_once()
        # first stall -> suspect, not yet confirmed (retry-once)
        self.assertEqual(rg.confirmed_from_log(log), set())

    def test_live_run_with_fresh_marker_is_left_alone(self):
        # Alive worker, and the in-flight record was marked just now -> still working,
        # never re-enqueue.
        import time as _t
        from tally_migrator.migration import resume
        with mock.patch.object(resume, "_job_alive", return_value=True), \
             mock.patch.object(resume.record_guard, "read_inflight",
                               return_value={"phase": "Customer", "ident": "Acme",
                                             "ts": _t.time()}), \
             mock.patch.object(resume, "_reenqueue") as reenq:
            resume._maybe_resume(self._row())
        reenq.assert_not_called()

    def test_stuck_marker_resumes_even_when_job_reports_alive(self):
        # No-fork case: a hard-killed worker leaves its RQ job stuck 'started' (job_alive
        # True), but the in-flight record has been marked far longer than the per-record
        # timeout, so the sweep still resumes it.
        import time as _t
        from tally_migrator.migration import resume
        log = self._log()
        stale_ts = _t.time() - (rg.record_timeout_s() + resume._RESUME_GRACE_S + 5)
        with mock.patch.object(resume, "_job_alive", return_value=True), \
             mock.patch.object(resume.record_guard, "read_inflight",
                               return_value={"phase": "Customer", "ident": "Acme",
                                             "ts": stale_ts}), \
             mock.patch.object(resume.frappe, "get_doc", return_value=log), \
             mock.patch.object(resume.frappe.db, "commit"), \
             mock.patch.object(resume, "_reenqueue") as reenq:
            resume._maybe_resume(self._row())
        reenq.assert_called_once()

    def test_reenqueue_failure_restores_marker_and_uncounts_stall(self):
        # If re-enqueue raises after the marker was cleared and the stall recorded, both
        # must be undone so a later sweep retries without double-counting the one hang.
        from tally_migrator.migration import resume
        log = self._log()
        restored = {}
        with mock.patch.object(resume, "_job_alive", return_value=False), \
             mock.patch.object(resume.record_guard, "read_inflight",
                               return_value={"phase": "Customer", "ident": "Acme"}), \
             mock.patch.object(resume.frappe, "get_doc", return_value=log), \
             mock.patch.object(resume.frappe.db, "commit"), \
             mock.patch.object(resume.record_guard, "set_inflight",
                               side_effect=lambda *a: restored.setdefault("set", a)), \
             mock.patch.object(resume, "_reenqueue", side_effect=RuntimeError("queue full")):
            with self.assertRaises(RuntimeError):
                resume._maybe_resume(self._row())
        # marker was restored, and the stall was un-counted (attempts back to 0 -> record
        # dropped from the hung set, so it is not one step closer to confirmed-skip).
        self.assertIn("set", restored)
        self.assertEqual(rg.confirmed_from_log(log), set())
        st = rg._read_state(log)
        self.assertEqual(st["resume_count"], 0)
        self.assertNotIn(rg._state_key("Customer", "Acme"), st["hung"])

    def test_no_marker_is_left_alone(self):
        from tally_migrator.migration import resume
        with mock.patch.object(resume, "_job_alive", return_value=False), \
             mock.patch.object(resume.record_guard, "read_inflight", return_value=None), \
             mock.patch.object(resume, "_reenqueue") as reenq:
            resume._maybe_resume(self._row())
        reenq.assert_not_called()

    def test_cap_exceeded_fails_run_without_reenqueue(self):
        from tally_migrator.migration import resume
        log = self._log()
        # pre-seed resume_count at the cap so the next stall trips it
        import json as _json
        log._d["guard_state"] = _json.dumps({"hung": {}, "resume_count": rg.max_resumes()})
        with mock.patch.object(resume, "_job_alive", return_value=False), \
             mock.patch.object(resume.record_guard, "read_inflight",
                               return_value={"phase": "Customer", "ident": "Acme"}), \
             mock.patch.object(resume.frappe, "get_doc", return_value=log), \
             mock.patch.object(resume.frappe, "log_error"), \
             mock.patch.object(resume.frappe.db, "commit"), \
             mock.patch.object(resume, "_reenqueue") as reenq:
            resume._maybe_resume(self._row())
        reenq.assert_not_called()
        self.assertEqual(log._d.get("status"), "Failed")


if __name__ == "__main__":
    unittest.main()
