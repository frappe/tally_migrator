"""Auto-resume of a migration a per-record hang killed.

When the hang guard (see ``record_guard``) dumps + hard-kills the worker, the run is
left ``Running`` with an in-flight marker in Redis naming the record it died on. This
scheduler sweep notices such a run, records the stall (retry-once, then confirm), and
re-enqueues it so it can continue: the import is idempotent, so already-committed
records are skipped, and a *confirmed*-hung record is skipped outright - the run steps
past the culprit and finishes, ending "Completed with Errors" with the skipped records
visible in the log.

A hard ``max_resumes`` cap makes a runaway impossible: past the cap the run is failed
for manual attention instead of resuming forever.
"""
import frappe

from tally_migrator.migration import record_guard


def resume_stalled_runs() -> None:
    """Scheduler entry point. Re-enqueue any run a per-record hang killed. Best-effort
    and defensive: one bad row must never break the sweep for the others."""
    try:
        rows = frappe.get_all(
            "Tally Migration Log",
            filters={"status": "Running"},
            fields=["name", "job_id", "company"],
        )
    except Exception:
        return
    for row in rows:
        try:
            _maybe_resume(row)
        except Exception as exc:
            frappe.log_error(
                f"resume_stalled_runs failed for {row.get('name')}: {exc}", "Tally Migrator")


def _job_alive(job_id: str) -> bool:
    # Reuse the same liveness rule the start-guard uses, so "is this run still alive"
    # is judged identically everywhere.
    from tally_migrator.api import _is_job_alive
    return bool(job_id) and _is_job_alive(job_id)


def _maybe_resume(row) -> None:
    # Only act on a run whose worker is genuinely gone.
    if _job_alive(row.get("job_id")):
        return
    marker = record_guard.read_inflight(row["name"])
    if not marker:
        # Died, but not inside a guarded record (e.g. OOM/redeploy between records).
        # Not this mechanism's job - leave it to the existing manual re-run.
        return
    phase = marker.get("phase") or ""
    ident = marker.get("ident") or ""

    log = frappe.get_doc("Tally Migration Log", row["name"])
    if not log.get("source_file"):
        # Can't re-run without the source; stop trying and surface it.
        record_guard.clear_inflight(log.name)
        _fail(log, "a record stalled the migration but the source file is no longer "
                   "stored, so it can't be resumed automatically - re-upload and re-run")
        return

    attempts, resume_count = record_guard.note_stall(log, phase, ident)
    if resume_count > record_guard.max_resumes():
        record_guard.clear_inflight(log.name)
        _fail(log, f"stopped after {resume_count - 1} automatic resumes (limit reached) - "
                   f"the migration keeps stalling; needs manual attention")
        frappe.db.commit()
        return

    # Note the stall on the log itself (the healthy resume process can write to the DB),
    # pointing at the captured all-thread stack, so the evidence is linked and auditable.
    verb = ("left out from here on" if attempts >= 2
            else "will be retried once before being skipped")
    _append_error(
        log, f"Run stalled on {phase} / {ident} (attempt {attempts}); {verb}. "
             f"Full stack captured at {record_guard.hang_dump_path(log.name)}")

    # Clear the marker BEFORE re-enqueuing so the resumed run starts clean; the new job
    # writes its own marker as it processes.
    record_guard.clear_inflight(log.name)
    frappe.db.commit()
    _reenqueue(log)
    frappe.logger().info(
        f"[Tally Migrator] resumed {log.name}: {phase}/{ident} attempts={attempts} "
        f"resume={resume_count}")


def _reenqueue(log) -> None:
    """Re-enqueue the SAME log (preserving its guard_state) as a background job,
    reusing _run_masters_job - which re-parses the source and runs into this log."""
    job_id = log.get("job_id") or f"tally-masters-{log.name}"
    log.db_set("job_id", job_id, update_modified=False)
    frappe.enqueue(
        "tally_migrator.api._run_masters_job",
        queue="long",
        timeout=4 * 60 * 60,
        job_id=job_id,
        file_url=log.source_file,
        erpnext_company=log.company,
        uom_overrides=log.get("uom_overrides") or "",
        validation_report=log.get("validation_report") or "",
        record_overrides=log.get("record_overrides") or "",
        coa_mode=log.get("coa_mode") or "reuse",
        posting_date=str(log.get("posting_date") or ""),
        log_name=log.name,
        created_uoms="",
    )


def _append_error(log, line: str) -> None:
    """Append a line to the log's plain-text error_log, so a stall and its captured
    stack are visible in the migration log itself."""
    try:
        existing = log.get("error_log") or ""
        log.db_set("error_log", (existing + "\n" + line).strip(), update_modified=False)
    except Exception:
        pass


def _fail(log, reason: str) -> None:
    frappe.log_error(f"{log.name}: {reason}", "Tally Migrator")
    try:
        log.db_set("status", "Failed", commit=True)
    except Exception:
        pass
