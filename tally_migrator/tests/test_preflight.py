"""Phase 2: the Step-3 pre-flight orchestrator (validate_masters_data).

The safety-critical guarantee: a scan that FAILS returns ``{"status": "failed"}`` -
never a bare/empty payload the UI could mistake for "clean". Also: small files compute
inline (ready), large files go async (running -> cached ready/failed), and the cache key
is unique per file version / edits / company / user. No parsing needed - the compute is
mocked so these test the orchestration + honest-state contract.
"""
import unittest
from types import SimpleNamespace
from unittest import mock

from tally_migrator import api


def _file(name="F1", modified="2026-01-01"):
    return SimpleNamespace(name=name, modified=modified)


class _FakeCache:
    def __init__(self, initial=None):
        self.store = dict(initial or {})
        self.sets = []

    def get_value(self, k):
        return self.store.get(k)

    def set_value(self, k, v, expires_in_sec=None):
        self.store[k] = v
        self.sets.append((k, v, expires_in_sec))


class TestPreflightOrchestrator(unittest.TestCase):
    def setUp(self):
        # Resolve/access are exercised elsewhere; here we isolate the orchestration.
        self.p_resolve = mock.patch.object(api, "_resolve_file_doc", return_value=_file())
        self.p_access = mock.patch.object(api, "_assert_file_access")
        self.p_only = mock.patch.object(api.frappe, "only_for")
        self.p_resolve.start(); self.p_access.start(); self.p_only.start()

    def tearDown(self):
        mock.patch.stopall()

    # -- small file: inline --
    def test_small_file_ready(self):
        with mock.patch.object(api, "_should_run_async", return_value=False), \
             mock.patch.object(api, "_compute_preflight",
                               return_value={"clean": True, "groups": [], "uom_issues": []}):
            out = api.validate_masters_data("/files/x.xml")
        self.assertEqual(out["status"], "ready")
        self.assertTrue(out["clean"])

    def test_small_file_failure_is_honest_not_clean(self):
        # The whole point of Phase 2: a crash must NOT look like "no issues".
        with mock.patch.object(api, "_should_run_async", return_value=False), \
             mock.patch.object(api, "_compute_preflight", side_effect=ValueError("bad xml")), \
             mock.patch.object(api.frappe, "log_error"):
            out = api.validate_masters_data("/files/x.xml")
        self.assertEqual(out["status"], "failed")
        self.assertIn("bad xml", out["error"])
        self.assertNotIn("clean", out)          # no payload that could read as clean

    # -- large file: async --
    def test_large_file_enqueues_and_returns_running(self):
        cache = _FakeCache()
        with mock.patch.object(api, "_should_run_async", return_value=True), \
             mock.patch.object(api.frappe, "cache", return_value=cache), \
             mock.patch.object(api.frappe, "enqueue") as enq:
            out = api.validate_masters_data("/files/big.xml")
        self.assertEqual(out["status"], "running")
        enq.assert_called_once()
        # running marker was cached so concurrent polls don't re-enqueue
        self.assertTrue(any(v.get("status") == "running" for _, v, _ in cache.sets))

    def test_large_file_returns_cached_ready_without_reenqueue(self):
        key = api._preflight_key(_file(), "", "", "")
        cache = _FakeCache({key: {"status": "ready", "clean": False, "groups": [1]}})
        with mock.patch.object(api, "_should_run_async", return_value=True), \
             mock.patch.object(api.frappe, "cache", return_value=cache), \
             mock.patch.object(api.frappe, "enqueue") as enq:
            out = api.validate_masters_data("/files/big.xml")
        self.assertEqual(out["status"], "ready")
        enq.assert_not_called()

    def test_large_file_returns_cached_failed(self):
        key = api._preflight_key(_file(), "", "", "")
        cache = _FakeCache({key: {"status": "failed", "error": "boom"}})
        with mock.patch.object(api, "_should_run_async", return_value=True), \
             mock.patch.object(api.frappe, "cache", return_value=cache), \
             mock.patch.object(api.frappe, "enqueue") as enq:
            out = api.validate_masters_data("/files/big.xml")
        self.assertEqual(out["status"], "failed")
        enq.assert_not_called()


class TestPreflightJob(unittest.TestCase):
    def test_job_caches_ready_on_success(self):
        cache = _FakeCache()
        with mock.patch.object(api.frappe, "cache", return_value=cache), \
             mock.patch.object(api, "_compute_preflight",
                               return_value={"clean": True, "groups": []}):
            api._run_preflight_job("K", "/f.xml", "", "", "")
        self.assertEqual(cache.store["K"]["status"], "ready")
        self.assertTrue(cache.store["K"]["clean"])

    def test_job_caches_failed_and_reraises(self):
        cache = _FakeCache()
        with mock.patch.object(api.frappe, "cache", return_value=cache), \
             mock.patch.object(api.frappe, "log_error"), \
             mock.patch.object(api, "_compute_preflight", side_effect=RuntimeError("nope")):
            with self.assertRaises(RuntimeError):
                api._run_preflight_job("K", "/f.xml", "", "", "")
        self.assertEqual(cache.store["K"]["status"], "failed")
        self.assertIn("nope", cache.store["K"]["error"])


class TestPreviewOrchestrator(unittest.TestCase):
    """Step-1 preview is status-wrapped like the Step-3 scan so a large file cannot time
    the web request out: small inline (ready), large async (running -> cached
    ready/failed), and a failure is reported honestly, never as an empty 'ready'."""

    def setUp(self):
        self.p_resolve = mock.patch.object(api, "_resolve_file_doc", return_value=_file())
        self.p_access = mock.patch.object(api, "_assert_file_access")
        self.p_only = mock.patch.object(api.frappe, "only_for")
        self.p_resolve.start(); self.p_access.start(); self.p_only.start()

    def tearDown(self):
        mock.patch.stopall()

    def test_small_file_ready_with_counts(self):
        with mock.patch.object(api, "_should_run_async", return_value=False), \
             mock.patch.object(api, "_compute_preview",
                               return_value={"customers": 3, "ledger_accounts": 5}):
            out = api.preview_masters_file("/files/x.xml")
        self.assertEqual(out["status"], "ready")
        self.assertEqual(out["customers"], 3)

    def test_small_file_failure_is_honest(self):
        with mock.patch.object(api, "_should_run_async", return_value=False), \
             mock.patch.object(api, "_compute_preview", side_effect=ValueError("bad xml")), \
             mock.patch.object(api.frappe, "log_error"):
            out = api.preview_masters_file("/files/x.xml")
        self.assertEqual(out["status"], "failed")
        self.assertIn("bad xml", out["error"])
        self.assertNotIn("customers", out)       # no partial counts on failure

    def test_large_file_enqueues_and_returns_running(self):
        cache = _FakeCache()
        with mock.patch.object(api, "_should_run_async", return_value=True), \
             mock.patch.object(api.frappe, "cache", return_value=cache), \
             mock.patch.object(api.frappe, "enqueue") as enq:
            out = api.preview_masters_file("/files/big.zip")
        self.assertEqual(out["status"], "running")
        enq.assert_called_once()
        self.assertTrue(any(v.get("status") == "running" for _, v, _ in cache.sets))

    def test_large_file_returns_cached_ready_without_reenqueue(self):
        key = api._preview_key(_file())
        cache = _FakeCache({key: {"status": "ready", "customers": 9}})
        with mock.patch.object(api, "_should_run_async", return_value=True), \
             mock.patch.object(api.frappe, "cache", return_value=cache), \
             mock.patch.object(api.frappe, "enqueue") as enq:
            out = api.preview_masters_file("/files/big.zip")
        self.assertEqual(out["status"], "ready")
        self.assertEqual(out["customers"], 9)
        enq.assert_not_called()

    def test_preview_key_independent_of_company_and_edits(self):
        # The preview counts depend on neither the company nor inline edits, so the key
        # is stable across them (unlike the pre-flight key) - one parse serves the step.
        self.assertEqual(api._preview_key(_file()), api._preview_key(_file()))


class TestPreviewJob(unittest.TestCase):
    def test_job_caches_ready_on_success(self):
        cache = _FakeCache()
        with mock.patch.object(api.frappe, "cache", return_value=cache), \
             mock.patch.object(api, "_compute_preview", return_value={"items": 7}):
            api._run_preview_job("K", "/f.xml")
        self.assertEqual(cache.store["K"]["status"], "ready")
        self.assertEqual(cache.store["K"]["items"], 7)

    def test_job_caches_failed_and_reraises(self):
        cache = _FakeCache()
        with mock.patch.object(api.frappe, "cache", return_value=cache), \
             mock.patch.object(api.frappe, "log_error"), \
             mock.patch.object(api, "_compute_preview", side_effect=RuntimeError("nope")):
            with self.assertRaises(RuntimeError):
                api._run_preview_job("K", "/f.xml")
        self.assertEqual(cache.store["K"]["status"], "failed")
        self.assertIn("nope", cache.store["K"]["error"])


class TestPreflightKey(unittest.TestCase):
    def test_key_changes_with_inputs(self):
        base = api._preflight_key(_file(), "", "Co", "2026-01-01")
        self.assertNotEqual(base, api._preflight_key(_file(modified="2026-02-02"), "", "Co", "2026-01-01"))
        self.assertNotEqual(base, api._preflight_key(_file(), '{"a":1}', "Co", "2026-01-01"))
        self.assertNotEqual(base, api._preflight_key(_file(), "", "Other", "2026-01-01"))
        self.assertEqual(base, api._preflight_key(_file(), "", "Co", "2026-01-01"))  # stable

    def test_error_message_is_short_and_safe(self):
        self.assertEqual(api._preflight_error_message(ValueError("")), "the file could not be read or validated")
        self.assertTrue(len(api._preflight_error_message(ValueError("x" * 500))) <= 200)


if __name__ == "__main__":
    unittest.main()
