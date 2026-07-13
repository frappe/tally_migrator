"""Turn a raw profiler report into ranked, plain-language findings.

The profiler captures *what happened* (per-phase timings, SQL shapes, memory). This module
answers *what's wrong* - it inspects that data and emits structured findings a non-expert
can act on ("Suppliers: a query runs once per record - 310k calls, 82% of the phase"),
ranked by severity and time impact. Pure and side-effect-free: it takes a report dict and
returns a list of dicts, so it is trivially testable and carries no Frappe dependency.

Every finding is business-data-free: it references SQL *shapes* (fingerprinted) and phase
names only, never record content - so findings are safe to show and to share, same as the
report they derive from.

Input tolerance: works on both ``RunProfiler.report()`` (rich: percentiles, per-op split,
commit/http detail) and the lighter ``RunProfiler.compact()`` streamed live, by reading
each metric defensively. Detectors that need a field the compact shape lacks simply skip.
"""
from __future__ import annotations

import math

# Severity ranking for sorting (higher = more urgent).
_SEV_RANK = {"high": 3, "medium": 2, "low": 1, "info": 0}


def _num(v, default=0.0) -> float:
    """Coerce to a finite float, else the default. Rejects NaN/Infinity too - JSON can
    round-trip those, and every downstream ``int()``/``round()`` would otherwise raise. All
    numeric reads go through here so the analyzer honours the profiler's never-raises
    contract even on corrupt or adversarial input."""
    if v is None:
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _phase_metrics(label: str, ph: dict) -> dict:
    """Normalise one phase from either report() or compact() into a common metric set, so
    the detectors don't each have to know both shapes."""
    wall = _num(ph.get("wall_s"))
    records = _num(ph.get("records")) or _num(ph.get("planned"))

    sql = ph.get("sql")
    if isinstance(sql, dict):                       # report() shape
        sql_count = _num(sql.get("count"))
        sql_time = _num(sql.get("time_s"))
    else:                                           # compact() shape: sql is an int
        sql_count = _num(sql)
        sql_time = _num(ph.get("sql_s"))

    http = ph.get("http")
    if isinstance(http, dict):
        http_count = _num(http.get("count"))
        http_time = _num(http.get("time_s"))
    else:
        http_count = _num(http)
        http_time = 0.0

    commits = ph.get("commits")
    commit_count = _num(commits.get("count")) if isinstance(commits, dict) else 0.0
    commit_time = _num(commits.get("time_s")) if isinstance(commits, dict) else 0.0

    prm = ph.get("per_record_ms")
    prm = prm if isinstance(prm, dict) else {}
    raw_top = ph.get("top_sql")
    top_sql = ([t for t in raw_top if isinstance(t, dict)]
               if isinstance(raw_top, (list, tuple)) else [])

    return {
        "label": label,
        "wall_s": wall,
        "records": records,
        "sql_count": sql_count,
        "sql_time_s": sql_time,
        "http_count": http_count,
        "http_time_s": http_time,
        "commit_count": commit_count,
        "commit_time_s": commit_time,
        "enqueues": _num(ph.get("enqueues")),
        "p50_ms": _num(prm.get("p50")),
        "p95_ms": _num(prm.get("p95")),
        "p99_ms": _num(prm.get("p99")),
        "avg_ms": _num(prm.get("avg")) or _num(ph.get("avg_ms")),
        "top_sql": top_sql,
    }


def _finding(code, severity, phase, title, detail, evidence=None) -> dict:
    return {"code": code, "severity": severity, "phase": phase,
            "title": title, "detail": detail, "evidence": evidence or {}}


def _safe(detector, *args) -> list:
    """Run one detector, swallowing any error so a single bad phase/metric never aborts the
    whole analysis. Returns [] on failure."""
    try:
        return detector(*args) or []
    except Exception:
        return []


# ── Detectors (each returns 0..n findings for one phase) ─────────────────────────

def _detect_n_plus_one(m: dict) -> list:
    """A single query shape running roughly once (or more) per record - the classic
    migration killer where a lookup/insert was not batched. Flagged when a top query's
    call count is at least ~1x the record count and it eats real time."""
    out = []
    records = m["records"]
    if records < 50:
        return out
    for t in m["top_sql"]:
        count = _num(t.get("count"))
        t_s = _num(t.get("time_s"))
        if count >= records * 1.5 and t_s >= 1.0:
            per_rec = round(count / records, 1)
            sev = "high" if t_s >= 0.25 * max(m["wall_s"], 0.001) else "medium"
            out.append(_finding(
                "n_plus_one", sev, m["label"],
                "A query runs once per record (N+1)",
                f"'{_short(t.get('q'))}' ran {int(count):,} times "
                f"(~{per_rec}x per record) and took {round(t_s, 1)}s. Batching or caching "
                f"this lookup/write would cut most of the phase's query time.",
                {"query": t.get("q"), "count": int(count), "time_s": round(t_s, 1),
                 "per_record": per_rec}))
            break   # one N+1 finding per phase is enough; it's the dominant one
    return out


def _detect_sql_bound(m: dict, n1_phases: set) -> list:
    """The phase spends most of its wall time inside SQL (not building docs / Python). If a
    specific N+1 was already named, don't double-report; this covers the general case."""
    if m["wall_s"] < 2 or m["label"] in n1_phases:
        return []
    share = m["sql_time_s"] / m["wall_s"] if m["wall_s"] else 0
    if share < 0.6:
        return []
    top = m["top_sql"][0] if m["top_sql"] else None
    detail = (f"{int(share * 100)}% of the phase ({round(m['sql_time_s'], 1)}s of "
              f"{round(m['wall_s'], 1)}s) was spent in SQL across {int(m['sql_count']):,} "
              f"queries.")
    if top:
        detail += f" Top shape: '{_short(top.get('q'))}' ({int(_num(top.get('count'))):,} calls)."
    return [_finding("sql_bound", "medium", m["label"], "Phase is SQL-bound", detail,
                     {"sql_pct": int(share * 100), "sql_count": int(m["sql_count"])})]


def _detect_enqueue_flood(m: dict) -> list:
    """Background jobs were enqueued during the phase - on import this is ERPNext's
    '>100 rows -> Submission Queue' path, which in a synchronous run never completes and is
    a known revert/hang hazard."""
    if m["enqueues"] < 1:
        return []
    return [_finding(
        "enqueue_flood", "medium", m["label"],
        "Background jobs enqueued mid-phase",
        f"{int(m['enqueues'])} background job(s) were enqueued during this phase. In a "
        f"synchronous migration these do not run inline - large submits route to a queue - "
        f"which can leave documents un-submitted or stall a revert.",
        {"enqueues": int(m["enqueues"])})]


def _detect_http_in_loop(m: dict) -> list:
    """External HTTP calls scaling with the record count - a per-record call to the GST
    portal / gravatar / an integration makes the phase network-bound and slow."""
    records = m["records"]
    if records < 50 or m["http_count"] < records * 0.5:
        return []
    return [_finding(
        "http_in_loop", "high", m["label"],
        "External HTTP call ~per record",
        f"{int(m['http_count']):,} external HTTP call(s) over {int(records):,} records "
        f"({round(m['http_time_s'], 1)}s). A network call per record makes this phase "
        f"network-bound; batch or disable the integration during migration.",
        {"http_count": int(m["http_count"]), "http_time_s": round(m["http_time_s"], 1)})]


def _detect_commit_heavy(m: dict) -> list:
    """A large share of the phase spent committing - usually too-frequent commits."""
    if m["wall_s"] < 2 or m["commit_time_s"] <= 0:
        return []
    share = m["commit_time_s"] / m["wall_s"]
    if share < 0.3:
        return []
    return [_finding(
        "commit_heavy", "low", m["label"], "Frequent commits",
        f"{int(share * 100)}% of the phase ({round(m['commit_time_s'], 1)}s across "
        f"{int(m['commit_count'])} commits) was spent committing. Larger commit batches "
        f"would reduce this.",
        {"commit_pct": int(share * 100), "commit_count": int(m["commit_count"])})]


def _detect_slow_outliers(m: dict) -> list:
    """A few records take far longer than the median - a heavy outlier (many child rows,
    a slow validation) rather than a systemic problem. Needs percentiles (report shape)."""
    p50, p99 = m["p50_ms"], m["p99_ms"]
    if p50 <= 0 or p99 < 50:
        return []
    if p99 < 5 * p50:
        return []
    return [_finding(
        "slow_outliers", "low", m["label"], "A few records are much slower",
        f"Most records took ~{round(p50, 1)}ms (median) but the slowest 1% took "
        f"{round(p99, 1)}ms+ - a handful of heavy outliers, not a systemic slowdown. See "
        f"the slowest records for which ones.",
        {"p50_ms": round(p50, 1), "p99_ms": round(p99, 1)})]


def _detect_memory(mem_trail: list, env: dict) -> list:
    """Memory pressure: peak RSS near/over the worker's cap (an OOM risk), read from the
    memory trail against the environment's memory limit."""
    if not mem_trail:
        return []
    peak = max((_num(s.get("peak_mb")) or _num(s.get("rss_mb")) for s in mem_trail
                if isinstance(s, dict)), default=0.0)
    cap = _num((env or {}).get("worker_memory_limit_mb"))
    if peak <= 0 or cap <= 0:
        return []
    ratio = peak / cap
    if ratio < 0.85:
        return []
    sev = "high" if ratio >= 0.95 else "medium"
    return [_finding(
        "memory_pressure", sev, "",
        "Memory peaked near the worker's limit",
        f"Peak memory {round(peak)}MB reached {int(ratio * 100)}% of the "
        f"{round(cap)}MB worker limit - an out-of-memory kill is likely. A larger plan, "
        f"or splitting the file, would avoid it.",
        {"peak_mb": round(peak), "cap_mb": round(cap), "ratio_pct": int(ratio * 100)})]


def _short(q, n: int = 90) -> str:
    s = str(q or "").strip()
    return (s[:n] + "...") if len(s) > n else s


def analyze(report: dict, mem_trail: list | None = None, env: dict | None = None) -> list:
    """Return ranked findings for a profiler report. ``report`` is ``{phase_label: phase}``
    from ``RunProfiler.report()`` or ``.compact()``. Ranked by severity, then by the
    phase's share of total wall time (biggest problems first). Always safe to share.

    Never raises: like the rest of the profiler it is best-effort, so a corrupt/adversarial
    profile degrades to fewer (or no) findings rather than an error. All numeric reads pass
    through ``_num`` (finite-only), each phase is analysed under its own guard, and the whole
    computation is wrapped as a final safety net."""
    try:
        return _analyze(report, mem_trail, env)
    except Exception:
        return []


def _analyze(report, mem_trail, env) -> list:
    if not isinstance(report, dict) or not report:
        # No per-phase data (e.g. an unstarted run) - a memory finding may still apply.
        return _rank(_detect_memory(mem_trail or [], env or {}), {})

    metrics = []
    for lbl, ph in report.items():
        if isinstance(ph, dict):
            try:
                metrics.append(_phase_metrics(lbl, ph))
            except Exception:
                continue          # one unparseable phase must not lose the others
    total_wall = sum(m["wall_s"] for m in metrics) or 1.0

    # First pass: the per-record N+1 detector also suppresses the generic sql_bound one.
    n1 = {}
    for m in metrics:
        n1[m["label"]] = _safe(_detect_n_plus_one, m)
    n1_phases = {lbl for lbl, fs in n1.items() if fs}

    findings = []
    for m in metrics:
        findings += n1[m["label"]]
        findings += _safe(_detect_sql_bound, m, n1_phases)
        findings += _safe(_detect_enqueue_flood, m)
        findings += _safe(_detect_http_in_loop, m)
        findings += _safe(_detect_commit_heavy, m)
        findings += _safe(_detect_slow_outliers, m)

    # A dominant slow phase is worth stating even with no specific cause, so the user knows
    # where the time went. Only when it is a clear majority and not already flagged.
    slowest = max(metrics, key=lambda m: m["wall_s"], default=None)
    if slowest and slowest["wall_s"] >= 10 and slowest["wall_s"] / total_wall >= 0.4 \
            and not any(f["phase"] == slowest["label"] for f in findings):
        share = int(slowest["wall_s"] / total_wall * 100)
        findings.append(_finding(
            "bottleneck_phase", "info", slowest["label"],
            "One phase dominates the run",
            f"'{slowest['label']}' took {round(slowest['wall_s'], 1)}s - {share}% of the "
            f"whole run. Optimisation effort is best spent here.",
            {"share_pct": share, "wall_s": round(slowest["wall_s"], 1)}))

    findings += _safe(_detect_memory, mem_trail or [], env or {})

    phase_share = {m["label"]: m["wall_s"] / total_wall for m in metrics}
    return _rank(findings, phase_share)


def _rank(findings: list, phase_share: dict) -> list:
    return sorted(
        findings,
        key=lambda f: (_SEV_RANK.get(f["severity"], 0),
                       phase_share.get(f["phase"], 0)),
        reverse=True)


def headline(findings: list) -> str:
    """One-line summary of the top finding, for a banner. '' when there are none."""
    if not findings:
        return ""
    top = findings[0]
    return top["title"] + (f" - {top['phase']}" if top.get("phase") else "")
