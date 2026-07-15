"""Generic per-record hang guard - no single record can freeze a whole migration.

Why a process-kill and not an in-process interrupt
---------------------------------------------------
A record can hang inside C-level code (a C library call, a regex) that holds the GIL
and never returns. Python signal handlers and thread-based interrupts CANNOT preempt
that - verified empirically: SIGALRM does not interrupt a running query and cannot
break a GIL-holding C loop. The only thing that can act while the GIL is held is
``faulthandler``'s C-level watchdog: even mid-hang it dumps every thread's stack (so
we learn *where* it hung) and hard-exits the worker.

Recovery is by resume, not by in-process skip (impossible for a C-level hang):
the scheduler sweep (see ``resume_stalled_runs``) re-enqueues the run; the import is
idempotent so already-committed records are skipped, and a *confirmed* repeatedly-
hanging record is skipped from its recorded identity. A record is only skipped after
it hangs **twice** (retry-once), so a one-off slow record is never dropped - protecting
data completeness.

Design constraints this module respects
---------------------------------------
- **Leaf module**: imports only stdlib + frappe, so both ``BaseImporter`` and the
  openings importers can use it with no import cycle.
- **Transparent when inactive**: with no active run guard (unit tests, direct importer
  calls) every entry point is a no-op, so existing behaviour is unchanged.
- **The in-flight marker is written *before* each record** (in Redis, which survives
  the hard kill), because at kill time the GIL is held and no Python - not even the
  marker write - can run. Resume reads that marker to learn the culprit.
"""
import contextlib
import faulthandler
import os

import frappe

# ── Configuration (site_config, with safe defaults) ─────────────────────────────
_DEFAULT_TIMEOUT_S = 60
_DEFAULT_MAX_RESUMES = 50


def record_timeout_s() -> int:
    """Seconds any one record may take before the worker is dumped + killed. Generous
    by design: legitimate records take milliseconds, so only a genuine hang trips it,
    and a false trip would cost a skipped record (a data-completeness loss), so we bias
    high. Override with ``tally_migrator_record_timeout`` in site_config."""
    try:
        v = int(frappe.conf.get("tally_migrator_record_timeout") or _DEFAULT_TIMEOUT_S)
        return v if v > 0 else _DEFAULT_TIMEOUT_S
    except Exception:
        return _DEFAULT_TIMEOUT_S


def max_resumes() -> int:
    """Hard cap on auto-resumes for one run, so a bug can never loop forever. Each
    confirmed hang costs ~2 resumes (retry-once), so the default tolerates ~25 distinct
    hanging records before halting for manual attention. Override with
    ``tally_migrator_max_resumes``."""
    try:
        v = int(frappe.conf.get("tally_migrator_max_resumes") or _DEFAULT_MAX_RESUMES)
        return v if v > 0 else _DEFAULT_MAX_RESUMES
    except Exception:
        return _DEFAULT_MAX_RESUMES


# ── Redis in-flight marker (survives the hard kill) ─────────────────────────────
def _marker_key(log_name: str) -> str:
    return f"tally_inflight:{log_name}"


def set_inflight(log_name: str, phase: str, ident: str) -> None:
    """Record 'this run is now processing (phase, ident)'. Best-effort: a marker
    failure must never disturb the import."""
    try:
        frappe.cache().set_value(
            _marker_key(log_name), {"phase": phase, "ident": ident}, expires_in_sec=86400)
    except Exception:
        pass


def read_inflight(log_name: str) -> dict | None:
    try:
        return frappe.cache().get_value(_marker_key(log_name)) or None
    except Exception:
        return None


def clear_inflight(log_name: str) -> None:
    try:
        frappe.cache().delete_value(_marker_key(log_name))
    except Exception:
        pass


def hang_dump_path(log_name: str) -> str:
    """Where faulthandler writes a hang's all-thread stack for this run - on the shared
    site filesystem, so the resume worker can read it after the kill."""
    return os.path.join(frappe.get_site_path("tally_migrator_hangs"), f"{log_name}.log")


# ── Active run guard (module-global; migrations are serialised) ──────────────────
class RecordGuard:
    """Per-run guard state: the open dump file, the timeout, and the set of records
    already *confirmed* hung (skip on sight). Created once per run by MasterMigrator
    and installed with ``activate`` so importers reach it without wiring it through
    every constructor. Only one migration runs at a time (single-active-run guard), so
    a module-global is safe and can never bleed across runs."""

    def __init__(self, log_name: str, confirmed: set[tuple[str, str]] | None = None,
                 timeout_s: int | None = None):
        self.log_name = log_name
        self.timeout_s = timeout_s if timeout_s is not None else record_timeout_s()
        # {(phase, ident)} that hung twice - skip these outright.
        self.confirmed = confirmed or set()
        self._dump_file = None
        self._armed = False   # re-entrancy guard: faulthandler has one global timer

    # -- dump file (opened lazily, kept open for the run) --
    def _dump_path(self) -> str:
        os.makedirs(frappe.get_site_path("tally_migrator_hangs"), exist_ok=True)
        return hang_dump_path(self.log_name)

    def _ensure_dump_file(self):
        if self._dump_file is None:
            # Line-buffered append so a dump is on disk before the hard _exit, and
            # readable by the resume worker (shared site filesystem).
            self._dump_file = open(self._dump_path(), "a", buffering=1)
        return self._dump_file

    def close(self):
        if self._dump_file is not None:
            try:
                self._dump_file.close()
            finally:
                self._dump_file = None

    def should_skip(self, phase: str, ident: str) -> bool:
        return (phase, ident) in self.confirmed


_active: RecordGuard | None = None


def activate(guard: RecordGuard) -> None:
    global _active
    _active = guard


def deactivate() -> None:
    """Tear down the run guard on normal completion or a normal exception. Clears the
    in-flight marker so a finished run is never mistaken for a stalled one. NOT reached
    on a hang (the worker is hard-killed) - which is exactly why the marker then survives
    for the resume sweep to find."""
    global _active
    if _active is not None:
        clear_inflight(_active.log_name)
        _active.close()
    _active = None


# ── Persisted guard state on the log (hung records + resume count) ───────────────
# One hidden Code field, ``guard_state``, holds the whole picture so a stalled record
# and the resume count survive across the kill/resume cycle and stay auditable.
_STATE_FIELD = "guard_state"


def _state_key(phase: str, ident: str) -> str:
    return f"{phase}\x1f{ident}"


def _read_state(log) -> dict:
    raw = log.get(_STATE_FIELD)
    if not raw:
        return {"hung": {}, "resume_count": 0}
    try:
        d = frappe.parse_json(raw)
        return {"hung": dict(d.get("hung") or {}),
                "resume_count": int(d.get("resume_count") or 0)}
    except Exception:
        return {"hung": {}, "resume_count": 0}


def confirmed_from_log(log) -> set:
    """The set of ``(phase, ident)`` that have hung twice - skip these on sight. A
    record must stall TWICE before it is skipped (retry-once), so a one-off slow record
    is never dropped."""
    st = _read_state(log)
    return {
        (v.get("phase"), v.get("ident"))
        for v in st["hung"].values() if int(v.get("attempts") or 0) >= 2
    }


def note_stall(log, phase: str, ident: str) -> tuple[int, int]:
    """Resume bookkeeping for a killed run, in one write: bump this record's hang count
    (1 = suspect/retry, >=2 = confirmed/skip) and the overall resume count. Returns
    ``(attempts_for_record, total_resume_count)``."""
    st = _read_state(log)
    k = _state_key(phase, ident)
    entry = st["hung"].get(k) or {"phase": phase, "ident": ident, "attempts": 0}
    entry["attempts"] = int(entry.get("attempts") or 0) + 1
    st["hung"][k] = entry
    st["resume_count"] = int(st.get("resume_count") or 0) + 1
    log.db_set(_STATE_FIELD, frappe.as_json(st), update_modified=False)
    return entry["attempts"], st["resume_count"]


def current() -> RecordGuard | None:
    return _active


def should_skip(phase: str, ident: str) -> bool:
    """True when this record is confirmed-hung and must be skipped. No-op (False) when
    no run guard is active, so direct/test callers are unaffected."""
    g = _active
    return bool(g and g.should_skip(phase, ident))


@contextlib.contextmanager
def guard(phase: str, ident: str):
    """Time-box one record. On overrun, faulthandler dumps every thread's stack to the
    run's hang log and hard-exits the worker; resume takes it from there.

    No-op when no run guard is active, or when already armed (re-entrancy: faulthandler
    has a single global timer, so a nested guard must not clobber the outer deadline).
    """
    g = _active
    if g is None or g._armed:
        yield
        return
    # Mark the in-flight record (Redis, survives the hard kill) so resume learns the
    # culprit. Deliberately no per-record write to the dump file: a clean run would
    # spew a line per record for nothing; the file gets content only when a hang fires.
    set_inflight(g.log_name, phase, str(ident))
    f = g._ensure_dump_file()
    faulthandler.dump_traceback_later(g.timeout_s, file=f, exit=True)
    g._armed = True
    try:
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()
        g._armed = False


def guarded_records(phase: str, records, ident_fn, on_skip=None):
    """Time-box each record of an importer that does NOT go through ``BaseImporter.run``
    (the openings loops), WITHOUT re-indenting its loop body.

    Because the guard is armed here and released when the caller asks for the next item,
    the ``with guard`` spans the caller's loop body (the generator is suspended at
    ``yield`` inside it). A confirmed-hung record is never yielded - so it cannot hang
    again - and ``on_skip(record, ident)`` fires so the caller can log the omission.
    ``break``/exceptions in the caller close the generator, which disarms cleanly."""
    for r in records:
        ident = str(ident_fn(r))
        if should_skip(phase, ident):
            if on_skip:
                on_skip(r, ident)
            continue
        with guard(phase, ident):
            yield r
