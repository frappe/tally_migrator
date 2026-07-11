"""Lightweight, crash-proof run profiler for the masters migration.

Purpose: make every run self-explain *where the time goes*, so performance work on
constrained workers (Frappe Cloud) is driven by data, not guesses. It captures, per
phase: wall time, per-record timing distribution (avg / p50 / p95 / p99 / max), a
sub-operation breakdown (build / upsert / address / contact / ...), SQL query count +
time, commit count + time, background-job enqueues, external HTTP calls + time, RSS,
and the slowest records *with their content* so a slow outlier can be inspected.

Design constraints (this runs inside the migration's critical path):
  * Near-zero overhead - plain counters/timers, no cProfile/tracemalloc. A phase that
    is not active makes the context managers no-ops.
  * Best-effort - every collection point swallows its own errors. A profiler bug must
    never affect what gets imported or fail a run.
  * Crash-proof - the orchestrator streams a compact snapshot to the durable progress
    cache each checkpoint, so a run that is OOM-killed/stalled still leaves its profile
    up to that point (same channel as the memory trail).

Usage (orchestrator):
    prof = RunProfiler(mem_fn=_rss_mb)
    with profiler.session(prof):                 # installs SQL/enqueue/HTTP hooks
        with prof.phase("Suppliers", count=n):   # one phase
            importer.run(records)                # records/ops collected via the
                                                 # module-level record()/op() helpers
    report = prof.report()                       # full structured report
    live   = prof.compact()                      # small snapshot for the cache

Usage (importers, hot path):
    from tally_migrator.migration import profiler
    with profiler.record(ident, content):
        with profiler.op("upsert"):
            ...
"""
from __future__ import annotations

import contextlib
import heapq
import os
import re
import resource
import sys
import threading
import time

# Active profiler is process-local. The worker runs one migration at a time (the
# single-active-run guard serialises them), so a thread-local holder is ample and
# keeps the hot-path helpers a single attribute read when no run is active.
_holder = threading.local()


def _active() -> "RunProfiler | None":
    return getattr(_holder, "prof", None)


# ── Resident-memory reading (shared by import, revert and the watchdog) ──────────

def rss_mb() -> tuple[float, float]:
    """``(current_rss_mb, peak_rss_mb)`` for this process.

    Current resident memory is read from ``/proc`` on Linux (the Frappe Cloud worker),
    falling back to the rusage peak where ``/proc`` is absent (e.g. macOS dev). A single
    cheap read with no allocation, safe on the migration's hot path and safe to call from
    the watchdog thread (it touches only ``/proc``/``rusage``, never Frappe). The single
    source of truth so import, revert and the watchdog all report memory identically."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # ru_maxrss is bytes on macOS, kibibytes on Linux.
    peak_mb = peak / 1048576 if sys.platform == "darwin" else peak / 1024
    try:
        with open("/proc/self/statm") as fh:
            resident_pages = int(fh.read().split()[1])
        cur_mb = resident_pages * os.sysconf("SC_PAGE_SIZE") / 1048576
    except Exception:
        cur_mb = peak_mb
    return round(cur_mb, 1), round(peak_mb, 1)


# ── Content capture (bounded) ──────────────────────────────────────────────────

_MAX_FIELDS = 25
_MAX_VALUE = 200


def _trim(content) -> dict | None:
    """A bounded copy of a record for slowest-record capture - never the live dict, and
    capped in field count + value length so the cached/stored profile stays small."""
    if not isinstance(content, dict):
        return None
    out = {}
    for i, (k, v) in enumerate(content.items()):
        if i >= _MAX_FIELDS:
            out["_truncated"] = True
            break
        if v in (None, "", [], {}):
            continue
        s = v if isinstance(v, str) else repr(v)
        out[k] = s[:_MAX_VALUE]
    return out


# ── SQL fingerprinting (which queries dominate a phase) ─────────────────────────
# Frappe passes templated queries (literals live in a separate values tuple, the
# string carries %s placeholders), so a query "fingerprint" is almost free to derive
# and collapses thousands of calls onto the handful of distinct statement shapes that
# actually drive a phase. That is what turns "322k queries" into "which 5 queries".
# Best-effort and bounded: a raw->fingerprint cache avoids recomputation, both the
# cache and the per-phase fingerprint table are size-capped so a pathological run
# can't grow them without bound, and any failure falls back to a single "?" bucket.
_FP_CACHE: dict[str, str] = {}
_FP_CACHE_MAX = 5000
_FP_STR = re.compile(r"'(?:[^'\\]|\\.)*'")          # inline string literal -> ?
_FP_NUM = re.compile(r"\b\d+\b")                    # inline number literal -> ?
_FP_LIST = re.compile(r"\(\s*(?:\?|%s)(?:\s*,\s*(?:\?|%s))+\s*\)")  # IN (?,?,..) -> (?)


def _fingerprint(query) -> str:
    try:
        q = query if isinstance(query, str) else str(query)
    except Exception:
        return "?"
    cached = _FP_CACHE.get(q)
    if cached is not None:
        return cached
    try:
        s = " ".join(q.split())          # collapse whitespace/newlines
        s = _FP_STR.sub("?", s)          # drop any inline string literals
        s = _FP_NUM.sub("?", s)          # drop any inline numeric literals
        s = _FP_LIST.sub("(?)", s)       # collapse variable-length IN lists
        if len(s) > 300:
            s = s[:300]
    except Exception:
        s = "?"
    if len(_FP_CACHE) < _FP_CACHE_MAX:
        _FP_CACHE[q] = s
    return s


# ── Per-phase accumulator ──────────────────────────────────────────────────────

class _Phase:
    def __init__(self, label: str):
        self.label = label
        self.planned = 0
        self.t_start = None          # set on phase enter, for live wall during a phase
        self.wall_ms = 0.0
        self.count = 0
        self.total_ms = 0.0
        self.max_ms = 0.0
        self.durations: list[float] = []          # capped, for percentiles
        self.slowest: list[tuple] = []            # min-heap of (ms, seq, ident, content)
        self.ops: dict[str, list] = {}            # name -> [count, total_ms]
        self.sql_count = 0
        self.sql_ms = 0.0
        self.sql_fp: dict[str, list] = {}         # fingerprint -> [count, total_ms]
        self.commit_count = 0
        self.commit_ms = 0.0
        self.enqueues = 0
        self.http_count = 0
        self.http_ms = 0.0
        self.rss_mb = 0.0
        self.peak_mb = 0.0
        self._seq = 0
        # The record currently being processed (set by record() on enter, cleared on
        # exit). Read by the watchdog thread so a run that hangs or is OOM-killed inside
        # one record still reveals *which* record was in flight - a single attribute read,
        # GIL-atomic, so no lock is needed on the hot path.
        self.current_ident = ""

    # hot path - keep cheap
    def add_record(self, ms: float, ident: str, content) -> None:
        self.count += 1
        self.total_ms += ms
        if ms > self.max_ms:
            self.max_ms = ms
        if len(self.durations) < 60000:
            self.durations.append(ms)
        self._seq += 1
        item = (ms, self._seq, ident, content)
        if len(self.slowest) < 15:
            heapq.heappush(self.slowest, item)
        elif ms > self.slowest[0][0]:
            heapq.heapreplace(self.slowest, item)

    def add_op(self, name: str, ms: float) -> None:
        o = self.ops.get(name)
        if o is None:
            self.ops[name] = [1, ms]
        else:
            o[0] += 1
            o[1] += ms

    # hot path - one dict lookup per query; table capped so it can't grow unbounded.
    def add_sql_fp(self, fp: str, ms: float) -> None:
        e = self.sql_fp.get(fp)
        if e is not None:
            e[0] += 1
            e[1] += ms
        elif len(self.sql_fp) < 2000:
            self.sql_fp[fp] = [1, ms]

    def _live_wall_ms(self) -> float:
        if self.wall_ms:
            return self.wall_ms
        return (time.monotonic() - self.t_start) * 1000 if self.t_start else 0.0

    def summary(self) -> dict:
        ds = sorted(self.durations)

        def pctl(p):
            if not ds:
                return 0.0
            return round(ds[min(len(ds) - 1, int(len(ds) * p))], 2)

        wall = self._live_wall_ms() or 1.0
        ops = {
            n: {
                "count": c,
                "total_s": round(t / 1000, 2),
                "avg_ms": round(t / c, 3) if c else 0,
                "pct_of_phase": round(100 * t / wall, 1),
            }
            for n, (c, t) in sorted(self.ops.items(), key=lambda x: -x[1][1])
        }
        return {
            "wall_s": round(self._live_wall_ms() / 1000, 2),
            "records": self.count,
            "planned": self.planned,
            "per_record_ms": {
                "avg": round(self.total_ms / self.count, 2) if self.count else 0,
                "p50": pctl(0.50), "p95": pctl(0.95), "p99": pctl(0.99),
                "max": round(self.max_ms, 2),
            },
            "ops": ops,
            "sql": {
                "count": self.sql_count,
                "time_s": round(self.sql_ms / 1000, 2),
                "per_record": round(self.sql_count / self.count, 1) if self.count else 0,
                "pct_of_phase": round(100 * self.sql_ms / wall, 1),
            },
            "top_sql": [
                {"count": c, "time_s": round(t / 1000, 2),
                 "avg_ms": round(t / c, 3) if c else 0, "q": fp}
                for fp, (c, t) in sorted(
                    self.sql_fp.items(), key=lambda x: -x[1][1])[:20]
            ],
            "commits": {"count": self.commit_count, "time_s": round(self.commit_ms / 1000, 2)},
            "enqueues": self.enqueues,
            "http": {"count": self.http_count, "time_s": round(self.http_ms / 1000, 2)},
            "rss_mb": self.rss_mb,
            "peak_mb": self.peak_mb,
            "slowest": [
                {"id": ident, "ms": round(ms, 1), "content": content}
                for (ms, _seq, ident, content) in sorted(self.slowest, reverse=True)
            ],
        }

    def compact(self) -> dict:
        """Small snapshot for the live cache stream (no durations/content)."""
        top_ops = sorted(((n, round(t / 1000, 2)) for n, (c, t) in self.ops.items()),
                         key=lambda x: -x[1])[:4]
        # Top few query shapes by time, so a stalled/never-finalised run still shows
        # (from the live cache) which SQL is eating the phase.
        top_sql = [
            {"count": c, "time_s": round(t / 1000, 1), "q": fp[:120]}
            for fp, (c, t) in sorted(self.sql_fp.items(), key=lambda x: -x[1][1])[:3]
        ]
        return {
            "wall_s": round(self._live_wall_ms() / 1000, 2),
            # ``planned`` is the phase's record count (always known); ``records`` is how
            # many were per-record timed - equal for the revert and base-loop importers,
            # 0 for importers with a custom run() where only phase-level SQL is captured.
            "planned": self.planned,
            "records": self.count,
            "avg_ms": round(self.total_ms / self.count, 2) if self.count else 0,
            "sql": self.sql_count,
            "sql_s": round(self.sql_ms / 1000, 1),
            "top_sql": top_sql,
            "enqueues": self.enqueues,
            "http": self.http_count,
            "rss_mb": self.rss_mb,
            "ops_s": dict(top_ops),
        }


class RunProfiler:
    def __init__(self, mem_fn=None):
        self.phases: dict[str, _Phase] = {}     # insertion-ordered
        self.current: _Phase | None = None
        self._mem_fn = mem_fn
        # Set by session(): the watchdog thread (or None) and whether tracemalloc is on.
        self.watchdog = None
        self.tracing = False
        self._stop_trace = False

    @contextlib.contextmanager
    def phase(self, label: str, count: int = 0):
        ph = self.phases.get(label)
        if ph is None:
            ph = _Phase(label)
            self.phases[label] = ph
        ph.planned = count or ph.planned
        ph.t_start = time.monotonic()
        prev = self.current
        self.current = ph
        try:
            yield ph
        finally:
            ph.wall_ms += (time.monotonic() - ph.t_start) * 1000
            if self._mem_fn:
                try:
                    ph.rss_mb, ph.peak_mb = self._mem_fn()
                except Exception:
                    pass
            self.current = prev

    def report(self) -> dict:
        return {lbl: ph.summary() for lbl, ph in self.phases.items()}

    def compact(self) -> dict:
        try:
            return {lbl: ph.compact() for lbl, ph in self.phases.items()}
        except Exception:
            return {}


# ── Module-level hot-path helpers (no-op when no run is active) ──────────────────

@contextlib.contextmanager
def record(ident: str = "", content=None):
    p = _active()
    ph = p.current if p else None
    if ph is None:
        yield
        return
    # Publish the in-flight record so the watchdog can name it if this record hangs or
    # the worker is killed inside it. Cleared in the finally, even on error.
    ph.current_ident = ident
    t0 = time.monotonic()
    try:
        yield
    finally:
        try:
            ph.add_record((time.monotonic() - t0) * 1000, ident, _trim(content))
        except Exception:
            pass
        ph.current_ident = ""


@contextlib.contextmanager
def op(name: str):
    p = _active()
    ph = p.current if p else None
    if ph is None:
        yield
        return
    t0 = time.monotonic()
    try:
        yield
    finally:
        try:
            ph.add_op(name, (time.monotonic() - t0) * 1000)
        except Exception:
            pass


# ── Global hooks: SQL / commit / enqueue / HTTP, attributed to the current phase ─

def _install_hooks(prof: "RunProfiler"):
    """Patch the few global call points whose volume drives migration cost, each
    attributed to the profiler's current phase. Returns a restore() that undoes every
    patch. Fully best-effort: any individual patch that can't be applied is skipped."""
    restores = []

    def _patch_attr(obj, name, make):
        try:
            orig = getattr(obj, name)
        except Exception:
            return
        # Was ``name`` the object's *own* attribute, or inherited from its class? For an
        # inherited method (e.g. Database.commit on a db instance) the pristine restore is
        # to remove the shadowing attribute we add, not to setattr the captured bound
        # method (which would leave a permanent instance attribute holding a stale ref).
        had_own = name in getattr(obj, "__dict__", {})

        def _restore():
            if had_own:
                setattr(obj, name, orig)
            else:
                try:
                    delattr(obj, name)
                except Exception:
                    setattr(obj, name, orig)

        try:
            setattr(obj, name, make(orig))
            restores.append(_restore)
        except Exception:
            pass

    import frappe

    # SQL - the dominant cost in most Frappe loops.
    db = getattr(frappe.local, "db", None)
    if db is not None:
        def make_sql(orig):
            def sql(*a, **k):
                ph = prof.current
                if ph is None:
                    return orig(*a, **k)
                t0 = time.monotonic()
                try:
                    return orig(*a, **k)
                finally:
                    ms = (time.monotonic() - t0) * 1000
                    ph.sql_count += 1
                    ph.sql_ms += ms
                    try:
                        q = a[0] if a else k.get("query")
                        if q is not None:
                            ph.add_sql_fp(_fingerprint(q), ms)
                    except Exception:
                        pass
            return sql
        _patch_attr(db, "sql", make_sql)

        def make_commit(orig):
            def commit(*a, **k):
                ph = prof.current
                if ph is None:
                    return orig(*a, **k)
                t0 = time.monotonic()
                try:
                    return orig(*a, **k)
                finally:
                    ph.commit_count += 1
                    ph.commit_ms += (time.monotonic() - t0) * 1000
            return commit
        _patch_attr(db, "commit", make_commit)

    # Background-job enqueues - catches the queue-flood class of problem automatically.
    def make_enq(orig):
        def enqueue(*a, **k):
            ph = prof.current
            if ph is not None:
                ph.enqueues += 1
            return orig(*a, **k)
        return enqueue
    _patch_attr(frappe, "enqueue", make_enq)

    # External HTTP (GST portal, gravatar, any integration) - catches the slow-network
    # class. Patched on requests.Session.request, through which Frappe's HTTP goes.
    try:
        import requests.sessions as _rs

        def make_req(orig):
            def request(self, *a, **k):
                ph = prof.current
                if ph is None:
                    return orig(self, *a, **k)
                t0 = time.monotonic()
                try:
                    return orig(self, *a, **k)
                finally:
                    ph.http_count += 1
                    ph.http_ms += (time.monotonic() - t0) * 1000
            return request
        _patch_attr(_rs.Session, "request", make_req)
    except Exception:
        pass

    def restore():
        for r in reversed(restores):
            try:
                r()
            except Exception:
                pass

    return restore


# ── Watchdog: a wall-clock memory + in-flight-record sampler ─────────────────────
# The per-record instrumentation above is event-driven: it only emits when a record
# finishes and the orchestrator next streams (every ~250 records / phase boundary). A
# run that hangs inside one record, or is OOM-killed mid-parse, therefore leaves its
# last sample *before* the stall. The watchdog closes that gap: a daemon thread samples
# RSS + the current phase + the in-flight record every few seconds and writes a
# heartbeat straight to Redis, so a killed/hung run still shows memory right up to the
# kill and names the record that was in flight.
#
# Thread-safety: the thread touches only rss_mb() (/proc, no Frappe) and a *raw* Redis
# client bound to the same connection pool but built on the main thread - Frappe's
# request-local (site/db) is never read off-thread. Keys are pre-namespaced by site on
# the main thread, so no make_key/frappe.local lookup happens in the worker thread. Fully
# best-effort: every step swallows its own errors; the thread is a daemon and self-stops.

_HEARTBEAT_TTL = 6 * 60 * 60


def _heartbeat_key(name: str) -> str:
    """Site-namespaced Redis key for a run's heartbeat, built on a thread that has
    frappe.local (the main thread). The watchdog only ever uses the finished string."""
    import frappe
    site = getattr(frappe.local, "site", "") or ""
    return f"tally_migrator|heartbeat|{site}|{name}"


def _raw_redis():
    """A raw redis client on Frappe's cache connection pool - no key prefixing, thread-
    safe, so the watchdog can write from its own thread. ``None`` if unavailable."""
    try:
        import redis
        import frappe
        return redis.Redis(connection_pool=frappe.cache().connection_pool)
    except Exception:
        return None


class _Watchdog:
    def __init__(self, prof: "RunProfiler", redis_client, key: str,
                 interval: float = 2.0, trail_max: int = 300):
        self._prof = prof
        self._redis = redis_client
        self._key = key
        self._interval = max(0.5, float(interval))
        self._trail_max = trail_max
        self._stop = threading.Event()
        self._thread = None
        self.trail: list[dict] = []
        self._peak = 0.0

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="tally-profiler-watchdog", daemon=True)
        self._thread.start()

    def _sample(self) -> dict:
        try:
            cur, peak = rss_mb()
        except Exception:
            cur = peak = 0.0
        self._peak = max(self._peak, peak, cur)
        ph = getattr(self._prof, "current", None)
        s = {
            "ts": round(time.time(), 1),
            "phase": getattr(ph, "label", "") if ph else "",
            "record": getattr(ph, "current_ident", "") if ph else "",
            "rss_mb": cur,
            "peak_mb": round(self._peak, 1),
        }
        self.trail.append(s)
        if len(self.trail) > self._trail_max:
            del self.trail[:-self._trail_max]
        return s

    def _flush(self, sample: dict):
        if self._redis is None or not self._key:
            return
        try:
            import json
            payload = json.dumps({"last": sample, "trail": self.trail[-60:]})
            self._redis.set(self._key, payload, ex=_HEARTBEAT_TTL)
        except Exception:
            pass

    def _loop(self):
        # Sample immediately (so a run killed in the first seconds still leaves one point),
        # then on the interval until stopped.
        while True:
            try:
                self._flush(self._sample())
            except Exception:
                pass
            if self._stop.wait(self._interval):
                break

    def stop(self):
        self._stop.set()
        t = self._thread
        if t is not None:
            try:
                t.join(timeout=self._interval + 1)
            except Exception:
                pass
        self._thread = None


def read_heartbeat(name: str) -> dict:
    """The last watchdog heartbeat (+ recent trail) for a run, read on the main thread.
    ``{}`` when there is none. Used by the diagnostics endpoint to show where a hung or
    OOM-killed run actually was when it stopped."""
    try:
        import json
        client = _raw_redis()
        if client is None:
            return {}
        val = client.get(_heartbeat_key(name))
        return json.loads(val) if val else {}
    except Exception:
        return {}


# ── Optional allocation tracing (tracemalloc) - off by default (overhead) ────────

def top_allocations(limit: int = 12) -> list[dict]:
    """Top allocation sites by retained size, when tracemalloc is running (it is started
    only when the caller opts in, e.g. via site config). ``[]`` otherwise. Pinpoints the
    object holding memory - the 'why' behind an OOM the phase memory curve only hints at."""
    try:
        import tracemalloc
        if not tracemalloc.is_tracing():
            return []
        snap = tracemalloc.take_snapshot()
        return [
            {"where": str(s.traceback[0]),
             "size_mb": round(s.size / 1048576, 2),
             "blocks": s.count}
            for s in snap.statistics("lineno")[:limit]
        ]
    except Exception:
        return []


@contextlib.contextmanager
def session(prof: "RunProfiler", heartbeat_name: str = "",
            heartbeat_interval: float = 2.0, trace: bool = False):
    """Make ``prof`` the active profiler and install the global hooks for the duration.
    Always restores the hooks and clears the active profiler, even on error.

    ``heartbeat_name`` (a log / revert / preview id) starts the watchdog: a daemon thread
    streaming RSS + the in-flight record to Redis so a killed/hung run stays observable.
    ``trace`` starts tracemalloc for allocation-level attribution (opt-in; has overhead).
    Both are best-effort - a failure to start either never blocks the run."""
    restore = _install_hooks(prof)
    _holder.prof = prof
    wd = None
    if heartbeat_name:
        try:
            wd = _Watchdog(prof, _raw_redis(), _heartbeat_key(heartbeat_name),
                           interval=heartbeat_interval)
            wd.start()
        except Exception:
            wd = None
    prof.watchdog = wd
    prof.tracing = False
    if trace:
        try:
            import tracemalloc
            if not tracemalloc.is_tracing():
                tracemalloc.start()
                prof._stop_trace = True
            prof.tracing = True
        except Exception:
            prof.tracing = False
    try:
        yield prof
    finally:
        try:
            if wd is not None:
                wd.stop()
        except Exception:
            pass
        if getattr(prof, "_stop_trace", False):
            prof._stop_trace = False       # reset so a reused profiler can't re-stop later
            try:
                import tracemalloc
                tracemalloc.stop()
            except Exception:
                pass
        try:
            restore()
        finally:
            _holder.prof = None
