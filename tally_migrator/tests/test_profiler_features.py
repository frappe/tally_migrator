"""Tests for the end-to-end profiler features (branch fc/e2e-profiler, never merged to
main): the shared RSS reader, the in-flight-record watchdog, tracemalloc gating, the
web-parse profile, the durable fail-log flush, and the diagnostics endpoint's env / error
/ privacy handling. All best-effort code paths are asserted to degrade, never raise."""
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import frappe

from tally_migrator.migration import profiler
from tally_migrator.migration import revert_monitor
from tally_migrator.migration.master_migrator import MasterMigrator, _trace_enabled


class TestSharedRss(unittest.TestCase):
    def test_rss_mb_returns_positive_pair(self):
        cur, peak = profiler.rss_mb()
        self.assertGreater(cur, 0)
        self.assertGreater(peak, 0)


class TestInFlightIdent(unittest.TestCase):
    def setUp(self):
        self._prev = getattr(profiler._holder, "prof", None)

    def tearDown(self):
        profiler._holder.prof = self._prev

    def test_record_sets_and_clears_current_ident(self):
        prof = profiler.RunProfiler()
        profiler._holder.prof = prof
        with prof.phase("Suppliers", 2):
            with profiler.record("ACME Traders", {"name": "ACME Traders"}):
                self.assertEqual(prof.current.current_ident, "ACME Traders")
            # Cleared on exit so a hang *between* records is not misattributed.
            self.assertEqual(prof.current.current_ident, "")

    def test_current_ident_cleared_even_on_error(self):
        prof = profiler.RunProfiler()
        profiler._holder.prof = prof
        with prof.phase("Items", 1):
            with self.assertRaises(ValueError):
                with profiler.record("Widget", {"name": "Widget"}):
                    raise ValueError("boom")
            self.assertEqual(prof.current.current_ident, "")


class TestWatchdog(unittest.TestCase):
    def test_sample_captures_phase_record_and_memory(self):
        prof = profiler.RunProfiler()
        profiler._holder.prof = prof
        try:
            with prof.phase("Items", 5):
                prof.current.current_ident = "Widget-42"
                wd = profiler._Watchdog(prof, None, "key")
                s = wd._sample()
                self.assertEqual(s["phase"], "Items")
                self.assertEqual(s["record"], "Widget-42")
                self.assertGreater(s["rss_mb"], 0)
        finally:
            profiler._holder.prof = None

    def test_flush_with_no_redis_is_safe(self):
        wd = profiler._Watchdog(profiler.RunProfiler(), None, "key")
        wd._flush({"rss_mb": 1})  # must not raise

    def test_session_starts_watchdog_and_writes_heartbeat(self):
        prof = profiler.RunProfiler(mem_fn=profiler.rss_mb)
        name = f"utest-{frappe.generate_hash(length=8)}"
        with profiler.session(prof, heartbeat_name=name, heartbeat_interval=0.5):
            self.assertIsNotNone(prof.watchdog)
            with prof.phase("Suppliers", 1):
                prof.current.current_ident = "ACME"
                # The watchdog may take its first sample before the phase is entered, so
                # poll until it has sampled *inside* the phase (up to a few seconds).
                hb = {}
                for _ in range(30):
                    hb = profiler.read_heartbeat(name)
                    if (hb.get("last") or {}).get("phase") == "Suppliers":
                        break
                    time.sleep(0.1)
        self.assertEqual((hb.get("last") or {}).get("phase"), "Suppliers")
        self.assertGreater(hb["last"]["rss_mb"], 0)
        # Thread is stopped once the session ends.
        self.assertIsNone(prof.watchdog._thread)

    def test_heartbeat_survives_after_session(self):
        # The whole point: a killed run's last position is readable after the fact.
        prof = profiler.RunProfiler(mem_fn=profiler.rss_mb)
        name = f"utest-{frappe.generate_hash(length=8)}"
        with profiler.session(prof, heartbeat_name=name, heartbeat_interval=0.5):
            with prof.phase("Party Openings", 1):
                for _ in range(30):
                    if (profiler.read_heartbeat(name).get("last") or {}).get("phase"):
                        break
                    time.sleep(0.1)
        # Readable after the session ends - the whole point for a killed run.
        self.assertTrue(profiler.read_heartbeat(name).get("last"))


def _is_hook_wrapper(fn):
    # A profiler SQL/commit hook has a make_* closure qualname; the real method does not.
    return "make_" in getattr(fn, "__qualname__", "")


class TestHookLifecycle(unittest.TestCase):
    """Invariants proven under stress: the global hooks are installed for the session and
    removed pristinely afterward (no leaked wrapper, no shadowing instance attribute), and
    the session tears down even when phase entry or the body raises."""

    def test_hooks_installed_then_pristinely_removed(self):
        db = frappe.local.db
        had_own_sql = "sql" in db.__dict__
        had_own_commit = "commit" in db.__dict__
        prof = profiler.RunProfiler(mem_fn=profiler.rss_mb)
        with profiler.session(prof):
            self.assertTrue(_is_hook_wrapper(db.sql))
            self.assertTrue(_is_hook_wrapper(db.commit))
            self.assertIs(profiler._active(), prof)
        # Wrapper gone (real leak check - identity is meaningless for bound methods).
        self.assertFalse(_is_hook_wrapper(db.sql))
        self.assertFalse(_is_hook_wrapper(db.commit))
        # Inherited attributes are not left shadowed on the instance.
        self.assertEqual("sql" in db.__dict__, had_own_sql)
        self.assertEqual("commit" in db.__dict__, had_own_commit)
        self.assertIsNone(profiler._active())

    def test_web_parse_tears_down_and_stores_on_body_exception(self):
        from tally_migrator import api
        with self.assertRaises(ValueError):
            with api._profiled_web_parse("Preview parse"):
                raise ValueError("body blew up")
        # Session restored, and the profile was still captured (memory at the failure).
        self.assertFalse(_is_hook_wrapper(frappe.local.db.sql))
        self.assertIsNone(profiler._active())
        cached = frappe.cache().get_value(f"tally_preview_profile:{frappe.session.user}")
        self.assertTrue(cached and "peak_mb" in cached)

    def test_web_parse_tears_down_when_phase_entry_fails(self):
        # If the session enters but the phase then fails to enter, the session must still
        # be torn down (the leak this guards against) - and the body must still run.
        from tally_migrator import api
        ran = {"body": False}
        orig_phase = profiler.RunProfiler.phase
        try:
            profiler.RunProfiler.phase = mock.Mock(side_effect=RuntimeError("phase boom"))
            with api._profiled_web_parse("Preview parse"):
                ran["body"] = True
        finally:
            profiler.RunProfiler.phase = orig_phase
        self.assertTrue(ran["body"])
        self.assertFalse(_is_hook_wrapper(frappe.local.db.sql))
        self.assertIsNone(profiler._active())


class TestTracemallocGate(unittest.TestCase):
    def test_top_allocations_empty_when_not_tracing(self):
        # Not tracing (default) -> empty, never raises.
        import tracemalloc
        was = tracemalloc.is_tracing()
        if was:
            tracemalloc.stop()
        try:
            self.assertEqual(profiler.top_allocations(), [])
        finally:
            if was:
                tracemalloc.start()

    def test_session_trace_flag_toggles_tracing(self):
        import tracemalloc
        if tracemalloc.is_tracing():
            tracemalloc.stop()
        prof = profiler.RunProfiler()
        with profiler.session(prof, trace=True):
            self.assertTrue(prof.tracing)
            self.assertTrue(tracemalloc.is_tracing())
            top = profiler.top_allocations(5)
            self.assertIsInstance(top, list)
        # Stopped again on exit (session started it, so session stops it).
        self.assertFalse(tracemalloc.is_tracing())

    def test_trace_enabled_reads_site_config(self):
        with mock.patch.object(frappe, "conf", {"tally_migrator_tracemalloc": 1}):
            self.assertTrue(_trace_enabled())
        with mock.patch.object(frappe, "conf", {}):
            self.assertFalse(_trace_enabled())


class TestStripReport(unittest.TestCase):
    def _report(self):
        return {
            "Suppliers": {
                "wall_s": 317.8, "records": 5000,
                "sql": {"count": 310000},
                "top_sql": [{"q": "select name from `tabSupplier` where name=%s"}],
                "slowest": [
                    {"id": "ACME Traders Pvt Ltd", "ms": 812.3,
                     "content": {"gstin": "27ABCDE1234F1Z5"}},
                    {"id": "Beta Corp", "ms": 640.1, "content": {"city": "Mumbai"}},
                ],
            }
        }

    def test_default_strips_record_content_and_ids(self):
        safe = revert_monitor._strip_report(self._report(), include_content=False)
        blob = frappe.as_json(safe)
        self.assertNotIn("gstin", blob)
        self.assertNotIn("ACME", blob)
        self.assertNotIn("Mumbai", blob)
        # Timings and SQL shapes are kept.
        self.assertEqual(safe["Suppliers"]["wall_s"], 317.8)
        self.assertEqual(safe["Suppliers"]["slowest"], [{"ms": 812.3}, {"ms": 640.1}])

    def test_include_content_keeps_records(self):
        full = revert_monitor._strip_report(self._report(), include_content=True)
        self.assertIn("gstin", frappe.as_json(full))


class TestEnvBlock(unittest.TestCase):
    def test_env_block_shape(self):
        env = revert_monitor._env_block({})
        self.assertIn("python", env)
        self.assertIn("worker_memory_limit_mb", env)
        self.assertIn("frappe_version", env)
        self.assertTrue(env["python"])

    def test_worker_memory_limit_none_or_float(self):
        val = revert_monitor._worker_memory_limit_mb()
        self.assertTrue(val is None or isinstance(val, float))


class TestErrorLogCapture(unittest.TestCase):
    def test_captures_tally_errors_and_gates_traceback(self):
        marker = f"tally_migrator utest {frappe.generate_hash(length=6)}"
        frappe.get_doc({
            "doctype": "Error Log",
            "method": "tally_migrator.migration.master_migrator.run",
            "error": f"Traceback...\n{marker}\nValueError: bad record data 27ABCDE",
        }).insert(ignore_permissions=True)
        frappe.db.commit()

        default = revert_monitor._related_error_logs("", None, include_content=False)
        self.assertGreaterEqual(default["count"], 1)
        # Default hides the traceback (which could carry record values).
        self.assertNotIn("traceback", default["entries"][0])
        self.assertNotIn("27ABCDE", frappe.as_json(default))
        # Exception class (a type name) is safe and surfaced.
        self.assertTrue(any(e.get("exception") == "ValueError" for e in default["entries"]))

        full = revert_monitor._related_error_logs("", None, include_content=True)
        self.assertIn("traceback", full["entries"][0])


class TestFailLogFlush(unittest.TestCase):
    @mock.patch("frappe.db")
    @mock.patch("frappe.log_error")
    def test_fail_log_persists_profile_snapshot(self, _log_err, _db):
        captured = {}

        class _Log:
            name = "TML-UTEST"
            def reload(self):
                pass
            def save(self, **k):
                captured["extracted_counts"] = self.extracted_counts
                captured["status"] = self.status

        m = MasterMigrator.__new__(MasterMigrator)
        m.log = _Log()
        m._timings = {"Suppliers": 12.3}
        m._mem_trail = [{"phase": "Suppliers", "rss_mb": 800}]
        m._alloc_top = []
        m._profiler = profiler.RunProfiler()
        with m._profiler.phase("Suppliers", 1):
            pass
        m._fail_log(RuntimeError("kaboom"))
        self.assertEqual(captured["status"], "Failed")
        ec = frappe.parse_json(captured["extracted_counts"])
        self.assertTrue(ec.get("_failed"))
        self.assertIn("Suppliers", ec.get("_phase_seconds", {}))
        self.assertIn("Suppliers", ec.get("_profile", {}))

    @mock.patch("frappe.db")
    @mock.patch("frappe.log_error")
    def test_fail_log_survives_missing_profiler(self, _log_err, _db):
        # A crash before the profiler is built must still record the failure.
        class _Log:
            name = "TML-UTEST2"
            def reload(self): pass
            def save(self, **k): pass
        m = MasterMigrator.__new__(MasterMigrator)
        m.log = _Log()
        m._timings = {}
        m._mem_trail = []
        m._alloc_top = []
        m._profiler = None  # not yet constructed
        # Must not raise even though _profiler.report() is impossible.
        m._fail_log(RuntimeError("early"))


class TestWebParseProfile(unittest.TestCase):
    def test_parse_profile_cached_with_peak(self):
        from tally_migrator import api
        with api._profiled_web_parse("Preview parse"):
            pass
        cached = frappe.cache().get_value(
            f"tally_preview_profile:{frappe.session.user}")
        self.assertTrue(cached)
        self.assertIn("peak_mb", cached)
        self.assertEqual(cached["label"], "Preview parse")

    def test_body_runs_even_if_profiling_setup_fails(self):
        from tally_migrator import api
        ran = {"body": False}
        with mock.patch.object(api.profiler, "session",
                               side_effect=RuntimeError("no redis")):
            with api._profiled_web_parse("Preview parse"):
                ran["body"] = True
        self.assertTrue(ran["body"], "endpoint body must run even if profiling fails")


class TestDiagnosticsReportIntegration(unittest.TestCase):
    def test_assembles_safe_payload_from_persisted_profile(self):
        company = frappe.get_all("Company", pluck="name", limit=1)
        company = company[0] if company else None
        log = frappe.get_doc({
            "doctype": "Tally Migration Log",
            "company": company,
            "tally_company": "UTest Co",
            "status": "Failed",
            "extracted_counts": frappe.as_json({
                "Suppliers": {"created": 3},
                "_phase_seconds": {"Suppliers": 9.9},
                "_failed": True,
                "_mem_trail": [{"phase": "Suppliers", "rss_mb": 850, "peak_mb": 900}],
                "_profile": {"Suppliers": {
                    "wall_s": 9.9,
                    "slowest": [{"id": "ACME 27ABCDE", "ms": 500,
                                 "content": {"gstin": "27ABCDE1234F1Z5"}}],
                }},
            }),
        }).insert(ignore_permissions=True)
        frappe.db.commit()

        out = revert_monitor.diagnostics_report(log.name, include_content=0)
        self.assertEqual(out["log"], log.name)
        self.assertTrue(out["failed"])
        self.assertIn("env", out)
        self.assertIn("errors", out)
        # Persisted memory trail is surfaced when the live cache is empty.
        self.assertTrue(out["mem_trail"])
        # Record content is stripped by default.
        self.assertNotIn("gstin", frappe.as_json(out))
        self.assertNotIn("27ABCDE", frappe.as_json(out))

        full = revert_monitor.diagnostics_report(log.name, include_content=1)
        self.assertIn("gstin", frappe.as_json(full))

    def test_findings_surface_in_report(self):
        # An N+1 profile must produce a ranked finding + headline in the endpoint payload.
        company = frappe.get_all("Company", pluck="name", limit=1)
        log = frappe.get_doc({
            "doctype": "Tally Migration Log",
            "company": company[0] if company else None,
            "tally_company": "UTest Co",
            "status": "Completed",
            "extracted_counts": frappe.as_json({
                "_phase_seconds": {"Suppliers": 300.0},
                "_profile": {"Suppliers": {
                    "wall_s": 300.0, "records": 5000,
                    "sql": {"count": 310000, "time_s": 245.0},
                    "top_sql": [{"count": 300000, "time_s": 240.0,
                                 "q": "select name from `tabSupplier` where name=%s"}],
                }},
            }),
        }).insert(ignore_permissions=True)
        frappe.db.commit()
        out = revert_monitor.diagnostics_report(log.name)
        self.assertTrue(out["findings"])
        self.assertEqual(out["findings"][0]["code"], "n_plus_one")
        self.assertIn("Suppliers", out["headline"])


class TestWatchdogIntervalConfig(unittest.TestCase):
    def test_interval_follows_site_config(self):
        with mock.patch.object(frappe, "conf", {"tally_migrator_watchdog_interval": 7}):
            self.assertEqual(profiler._config_interval(), 7.0)
        with mock.patch.object(frappe, "conf", {}):
            self.assertEqual(profiler._config_interval(), 2.0)

    def test_session_uses_config_interval_when_unset(self):
        with mock.patch.object(frappe, "conf", {"tally_migrator_watchdog_interval": 5}):
            prof = profiler.RunProfiler(mem_fn=profiler.rss_mb)
            name = f"utest-{frappe.generate_hash(length=8)}"
            with profiler.session(prof, heartbeat_name=name):
                self.assertEqual(prof.watchdog._interval, 5.0)


if __name__ == "__main__":
    unittest.main()
