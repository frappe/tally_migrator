"""Tests for the findings engine (profiler_analysis) - the layer that turns a raw profile
into ranked, plain-language diagnoses. Pure functions, so these are fast and exhaustive:
each detector gets a positive and a negative case, both profile shapes are covered, and the
privacy invariant (SQL shapes only, no record content) is asserted."""
import json
import unittest

from tally_migrator.migration import profiler_analysis as pa


def codes(findings):
    return [f["code"] for f in findings]


class TestNPlusOne(unittest.TestCase):
    def test_fires_on_per_record_query(self):
        report = {"Suppliers": {
            "wall_s": 300.0, "records": 5000,
            "sql": {"count": 310000, "time_s": 245.0},
            "top_sql": [{"count": 300000, "time_s": 240.0,
                         "q": "select name from `tabSupplier` where name=%s"}],
        }}
        f = pa.analyze(report)
        self.assertIn("n_plus_one", codes(f))
        self.assertEqual(f[0]["severity"], "high")   # dominant -> high
        self.assertEqual(f[0]["evidence"]["per_record"], 60.0)

    def test_silent_when_query_is_batched(self):
        # One insert per record but cheap and not dominating -> no N+1.
        report = {"Items": {
            "wall_s": 12.0, "records": 5000,
            "sql": {"count": 5000, "time_s": 1.0},
            "top_sql": [{"count": 5000, "time_s": 0.8, "q": "insert into `tabItem`"}],
        }}
        self.assertNotIn("n_plus_one", codes(pa.analyze(report)))

    def test_needs_enough_records(self):
        report = {"Tiny": {"wall_s": 5.0, "records": 10,
                           "sql": {"count": 400, "time_s": 4.0},
                           "top_sql": [{"count": 400, "time_s": 4.0, "q": "select 1"}]}}
        self.assertNotIn("n_plus_one", codes(pa.analyze(report)))


class TestSqlBound(unittest.TestCase):
    def test_fires_when_most_time_in_sql(self):
        # Many distinct-ish queries, SQL dominant, but no single per-record query -> sql_bound.
        report = {"Openings": {
            "wall_s": 100.0, "records": 40,
            "sql": {"count": 800, "time_s": 80.0},
            "top_sql": [{"count": 40, "time_s": 40.0, "q": "select ... join ..."}],
        }}
        c = codes(pa.analyze(report))
        self.assertIn("sql_bound", c)
        self.assertNotIn("n_plus_one", c)

    def test_silent_when_cpu_bound(self):
        report = {"Build": {"wall_s": 50.0, "records": 1000,
                            "sql": {"count": 1000, "time_s": 5.0}, "top_sql": []}}
        self.assertNotIn("sql_bound", codes(pa.analyze(report)))


class TestEnqueueFlood(unittest.TestCase):
    def test_fires_on_any_enqueue(self):
        report = {"Opening Stock": {"wall_s": 30.0, "records": 500,
                                    "sql": {"count": 1000, "time_s": 5}, "enqueues": 3,
                                    "top_sql": []}}
        self.assertIn("enqueue_flood", codes(pa.analyze(report)))

    def test_silent_without_enqueues(self):
        report = {"Opening Stock": {"wall_s": 30.0, "records": 500,
                                    "sql": {"count": 1000, "time_s": 5}, "enqueues": 0,
                                    "top_sql": []}}
        self.assertNotIn("enqueue_flood", codes(pa.analyze(report)))


class TestHttpInLoop(unittest.TestCase):
    def test_fires_when_http_scales_with_records(self):
        report = {"Party Openings": {"wall_s": 120.0, "records": 3000,
                                     "sql": {"count": 9000, "time_s": 20},
                                     "http": {"count": 3000, "time_s": 80}, "top_sql": []}}
        f = pa.analyze(report)
        self.assertIn("http_in_loop", codes(f))
        self.assertTrue(any(x["code"] == "http_in_loop" and x["severity"] == "high" for x in f))

    def test_silent_with_few_http(self):
        report = {"Party Openings": {"wall_s": 120.0, "records": 3000,
                                     "sql": {"count": 9000, "time_s": 20},
                                     "http": {"count": 5, "time_s": 1}, "top_sql": []}}
        self.assertNotIn("http_in_loop", codes(pa.analyze(report)))


class TestCommitHeavy(unittest.TestCase):
    def test_fires_when_committing_dominates(self):
        report = {"Items": {"wall_s": 20.0, "records": 5000,
                            "sql": {"count": 5000, "time_s": 4},
                            "commits": {"count": 5000, "time_s": 8}, "top_sql": []}}
        self.assertIn("commit_heavy", codes(pa.analyze(report)))


class TestSlowOutliers(unittest.TestCase):
    def test_fires_when_p99_far_above_p50(self):
        report = {"Customers": {"wall_s": 60.0, "records": 4000,
                                "sql": {"count": 8000, "time_s": 10}, "top_sql": [],
                                "per_record_ms": {"p50": 10, "p99": 400}}}
        self.assertIn("slow_outliers", codes(pa.analyze(report)))

    def test_silent_when_uniform(self):
        report = {"Customers": {"wall_s": 60.0, "records": 4000,
                                "sql": {"count": 8000, "time_s": 10}, "top_sql": [],
                                "per_record_ms": {"p50": 12, "p99": 20}}}
        self.assertNotIn("slow_outliers", codes(pa.analyze(report)))


class TestMemoryPressure(unittest.TestCase):
    def test_fires_near_cap(self):
        f = pa.analyze({}, mem_trail=[{"rss_mb": 490, "peak_mb": 500}],
                       env={"worker_memory_limit_mb": 512.0})
        self.assertEqual(codes(f), ["memory_pressure"])
        self.assertEqual(f[0]["severity"], "high")

    def test_silent_with_headroom(self):
        f = pa.analyze({}, mem_trail=[{"rss_mb": 200, "peak_mb": 250}],
                       env={"worker_memory_limit_mb": 512.0})
        self.assertEqual(f, [])

    def test_silent_without_cap(self):
        f = pa.analyze({}, mem_trail=[{"rss_mb": 900, "peak_mb": 950}], env={})
        self.assertEqual(f, [])


class TestCompactShape(unittest.TestCase):
    def test_analyzes_live_compact_snapshot(self):
        # compact(): sql is an int, sql_s separate, records may be 0 for custom-run phases.
        compact = {"Opening Stock": {"wall_s": 188.0, "planned": 355000, "records": 0,
                                     "sql": 355000, "sql_s": 150.0, "enqueues": 3,
                                     "top_sql": [{"count": 355000, "time_s": 140.0,
                                                  "q": "select ... from `tabBin`"}]}}
        c = codes(pa.analyze(compact))
        self.assertIn("enqueue_flood", c)
        self.assertIn("sql_bound", c)   # records=0 -> N+1 can't compute, sql_bound covers it


class TestRankingAndSafety(unittest.TestCase):
    def test_high_severity_ranks_first(self):
        report = {
            "Suppliers": {"wall_s": 300.0, "records": 5000,
                          "sql": {"count": 310000, "time_s": 245.0},
                          "top_sql": [{"count": 300000, "time_s": 240.0,
                                       "q": "select name from `tabSupplier` where name=%s"}]},
            "Items": {"wall_s": 20.0, "records": 5000,
                      "sql": {"count": 5000, "time_s": 4},
                      "commits": {"count": 5000, "time_s": 8}, "top_sql": []},
        }
        f = pa.analyze(report)
        self.assertEqual(f[0]["severity"], "high")
        self.assertGreaterEqual(pa._SEV_RANK[f[0]["severity"]], pa._SEV_RANK[f[-1]["severity"]])

    def test_headline_is_top_finding(self):
        report = {"Suppliers": {"wall_s": 300.0, "records": 5000,
                                "sql": {"count": 310000, "time_s": 245.0},
                                "top_sql": [{"count": 300000, "time_s": 240.0,
                                             "q": "select name from `tabSupplier`=%s"}]}}
        f = pa.analyze(report)
        self.assertIn("Suppliers", pa.headline(f))

    def test_no_record_content_leaks(self):
        # Even given a report that (hypothetically) carried content, findings never echo it.
        report = {"Suppliers": {"wall_s": 300.0, "records": 5000,
                                "sql": {"count": 310000, "time_s": 245.0},
                                "top_sql": [{"count": 300000, "time_s": 240.0,
                                             "q": "select name from `tabSupplier` where name=%s"}],
                                "slowest": [{"id": "ACME 27ABCDE", "ms": 900,
                                             "content": {"gstin": "27ABCDE1234F1Z5"}}]}}
        blob = json.dumps(pa.analyze(report))
        self.assertNotIn("27ABCDE", blob)
        self.assertNotIn("ACME", blob)

    def test_empty_and_garbage_inputs(self):
        self.assertEqual(pa.analyze({}), [])
        self.assertEqual(pa.analyze(None), [])
        self.assertEqual(pa.analyze({"X": "not a dict"}), [])
        self.assertEqual(pa.headline([]), "")


class TestNeverRaises(unittest.TestCase):
    """The analyzer honours the profiler's best-effort contract: corrupt or adversarial
    input (non-finite floats, wrong-typed fields, garbage top_sql) degrades to fewer
    findings, never an exception."""

    def test_non_finite_numbers_do_not_raise(self):
        inf, nan = float("inf"), float("nan")
        report = {"P": {"wall_s": inf, "records": nan, "planned": inf,
                        "sql": {"count": nan, "time_s": inf},
                        "top_sql": [{"count": inf, "time_s": nan, "q": None}],
                        "per_record_ms": {"p50": nan, "p99": inf}}}
        out = pa.analyze(report, mem_trail=[{"rss_mb": inf, "peak_mb": nan}],
                         env={"worker_memory_limit_mb": inf})
        self.assertIsInstance(out, list)
        # And no NaN/inf leaks into any numeric evidence.
        import math
        for f in out:
            for v in f.get("evidence", {}).values():
                if isinstance(v, float):
                    self.assertTrue(math.isfinite(v))

    def test_wrong_typed_fields_do_not_raise(self):
        for bad in [
            {"P": {"wall_s": "x", "sql": "y", "top_sql": 5, "per_record_ms": [1, 2]}},
            {"P": {"top_sql": "not-a-list", "http": [1], "commits": "z"}},
            {"P": {"records": [1, 2, 3], "enqueues": {}}},
        ]:
            self.assertIsInstance(pa.analyze(bad), list)


if __name__ == "__main__":
    unittest.main()
