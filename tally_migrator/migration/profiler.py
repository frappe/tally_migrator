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
import threading
import time

# Active profiler is process-local. The worker runs one migration at a time (the
# single-active-run guard serialises them), so a thread-local holder is ample and
# keeps the hot-path helpers a single attribute read when no run is active.
_holder = threading.local()


def _active() -> "RunProfiler | None":
    return getattr(_holder, "prof", None)


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
        self.commit_count = 0
        self.commit_ms = 0.0
        self.enqueues = 0
        self.http_count = 0
        self.http_ms = 0.0
        self.rss_mb = 0.0
        self.peak_mb = 0.0
        self._seq = 0

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
        return {
            "wall_s": round(self._live_wall_ms() / 1000, 2),
            "records": self.count,
            "avg_ms": round(self.total_ms / self.count, 2) if self.count else 0,
            "sql": self.sql_count,
            "sql_s": round(self.sql_ms / 1000, 1),
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
    t0 = time.monotonic()
    try:
        yield
    finally:
        try:
            ph.add_record((time.monotonic() - t0) * 1000, ident, _trim(content))
        except Exception:
            pass


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
        try:
            setattr(obj, name, make(orig))
            restores.append(lambda: setattr(obj, name, orig))
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
                    ph.sql_count += 1
                    ph.sql_ms += (time.monotonic() - t0) * 1000
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


@contextlib.contextmanager
def session(prof: "RunProfiler"):
    """Make ``prof`` the active profiler and install the global hooks for the duration.
    Always restores the hooks and clears the active profiler, even on error."""
    restore = _install_hooks(prof)
    _holder.prof = prof
    try:
        yield prof
    finally:
        try:
            restore()
        finally:
            _holder.prof = None
