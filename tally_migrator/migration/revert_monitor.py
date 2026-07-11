"""Diagnostics-only monitor for a migration revert (branch: test/revert-with-profiler).

Reads the crash-proof progress cache ``run_revert`` streams to and prints the live
profile - percent, deleted/total, and the compact profiler snapshot (top SQL shapes by
time, per-record avg, commit/enqueue counts, RSS). Use it in another terminal while a
revert runs to watch where the time goes, without touching the running job.

    bench --site <site> execute tally_migrator.migration.revert_monitor.show
    bench --site <site> execute tally_migrator.migration.revert_monitor.show \
        --kwargs "{'revert_name': 'TMR-2026-00001'}"

Everything is wrapped so a bug here never raises through ``bench execute`` (which would
otherwise mask the real error); on failure it prints a traceback and returns a dict.
Never merged to main.
"""
import json
import traceback

import frappe


def _latest_revert() -> str | None:
    rows = frappe.get_all("Tally Migration Revert",
                          fields=["name", "status", "deleted_count", "kept_count"],
                          order_by="creation desc", limit=1)
    if not rows:
        return None
    r = rows[0]
    print(f"Latest revert: {r.name}  status={r.status}  "
          f"deleted={r.deleted_count}  kept={r.kept_count}")
    return r.name


def show(revert_name: str | None = None) -> dict:
    """Print the live progress + compact profile for a revert; return it as a dict."""
    try:
        revert_name = revert_name or _latest_revert()
        if not revert_name:
            print("No Tally Migration Revert documents yet.")
            return {}
        data = frappe.cache().get_value(f"tally_revert_progress:{revert_name}")
        if not data:
            print(f"No live profile cached for {revert_name} "
                  "(revert not started, or already expired).")
            return {}
        print(json.dumps(data, indent=2, default=str))
        return data
    except Exception:
        traceback.print_exc()
        return {"error": True}


def report(revert_name: str | None = None) -> dict:
    """Print the FULL profiler report if the revert doc kept one; else the live compact.

    ``run_revert`` streams only the compact snapshot to the cache (small, crash-proof).
    The full per-phase report - percentiles, top-20 SQL, slowest records with content -
    is available live only from within the worker, so here we surface the richest thing
    persisted: the final cached snapshot plus the revert's own counts and status.
    """
    try:
        revert_name = revert_name or _latest_revert()
        if not revert_name:
            print("No Tally Migration Revert documents yet.")
            return {}
        doc = frappe.get_doc("Tally Migration Revert", revert_name)
        out = {
            "revert": revert_name,
            "status": doc.status,
            "deleted_count": doc.deleted_count,
            "kept_count": doc.kept_count,
            "records_rows": len(doc.records or []),
            "live_profile": frappe.cache().get_value(f"tally_revert_progress:{revert_name}"),
        }
        print(json.dumps(out, indent=2, default=str))
        return out
    except Exception:
        traceback.print_exc()
        return {"error": True}


def queues() -> dict:
    """Diagnostics: per-queue depth + a breakdown of pending jobs by site/method, using
    frappe's configured RQ connection. Helps spot a foreign-site backlog starving a run."""
    import traceback
    from collections import Counter
    try:
        from frappe.utils.background_jobs import get_queues
        out = {}
        for q in get_queues():
            jobs = q.jobs
            by_site, by_method = Counter(), Counter()
            for j in jobs:
                kw = (j.kwargs or {})
                by_site[kw.get("site", "?")] += 1
                by_method[(j.kwargs or {}).get("method") or j.func_name or "?"] += 1
            out[q.name] = {"count": len(jobs),
                           "by_site": dict(by_site.most_common(5)),
                           "by_method": dict(by_method.most_common(5))}
        import json
        print(json.dumps(out, indent=2, default=str))
        return out
    except Exception:
        traceback.print_exc()
        return {"error": True}


def purge_site(site: str) -> dict:
    """Diagnostics: remove PENDING jobs belonging to ``site`` from every RQ queue (does
    not touch the running job or other sites). Used to clear a stale test site's backlog
    that is starving the single shared worker. Returns per-queue removed counts."""
    import traceback
    try:
        from frappe.utils.background_jobs import get_queues
        from rq.job import Job
        removed, skipped = {}, 0
        for q in get_queues():
            n = 0
            for jid in list(q.job_ids):     # iterate ids, fetch each defensively
                try:
                    j = Job.fetch(jid, connection=q.connection)
                    if (j.kwargs or {}).get("site") == site:
                        j.delete()
                        n += 1
                except Exception:
                    # Corrupted/expired job: drop its id from the queue directly.
                    try:
                        q.remove(jid)
                    except Exception:
                        skipped += 1
            removed[q.name] = n
        print(f"Removed pending jobs for {site}: {removed} (unreadable skipped: {skipped})")
        return removed
    except Exception:
        traceback.print_exc()
        return {"error": True}


def try_cancel(name: str = "ACC-JV-2026-00001", in_import: int = 0) -> dict:
    """Diagnostics: replicate _delete_one's cancel+delete on a Journal Entry inside a
    savepoint (optionally under frappe.flags.in_import, as _quiet_framework sets), surface
    the REAL exception (bench execute otherwise masks it), then roll back so the site is
    unchanged. Used to explain why a revert keeps a voucher."""
    import traceback
    prev = frappe.flags.in_import
    frappe.flags.in_import = bool(in_import)
    frappe.db.savepoint("dbg_cancel")
    try:
        doc = frappe.get_doc("Journal Entry", name)
        print(f"in_import={frappe.flags.in_import} docstatus_before={doc.docstatus}")
        doc.cancel()
        after = frappe.db.get_value("Journal Entry", name, "docstatus")
        print(f"after cancel: in-mem docstatus={doc.docstatus}  db docstatus={after}")
        frappe.delete_doc("Journal Entry", name, ignore_permissions=True,
                          delete_permanently=True)
        print("DELETE SUCCEEDED (rolling back to leave site unchanged)")
        frappe.db.rollback(save_point="dbg_cancel")
        return {"ok": True}
    except Exception as exc:
        frappe.db.rollback(save_point="dbg_cancel")
        print("FAILED:", type(exc).__name__)
        traceback.print_exc()
        return {"error": type(exc).__name__, "msg": str(exc)[:300]}
    finally:
        frappe.flags.in_import = prev


def try_delete_one(doctype: str = "Journal Entry", name: str = "ACC-JV-2026-00001") -> dict:
    """Diagnostics: run the real _delete_one on one record inside a savepoint under the
    same in_import flag the revert uses, then roll back so the site is unchanged. Confirms
    whether the synchronous-_cancel fix lets a large JE actually delete."""
    import traceback
    from tally_migrator.migration import rollback
    prev = frappe.flags.in_import
    frappe.flags.in_import = True
    frappe.db.savepoint("dbg_del")
    try:
        reason = rollback._delete_one({"doctype": doctype, "name": name}, set())
        print("reason:", reason if reason else "None (DELETED OK)")
        frappe.db.rollback(save_point="dbg_del")   # leave site unchanged
        return {"reason": reason}
    except Exception:
        frappe.db.rollback(save_point="dbg_del")
        traceback.print_exc()
        return {"error": True}
    finally:
        frappe.flags.in_import = prev


def try_delete_one_unlocked(doctype: str = "Journal Entry", name: str = "ACC-JV-2026-00001") -> dict:
    """Diagnostics: clear any stale lock, then run the real _delete_one under in_import in a
    savepoint and roll back. Confirms the synchronous-_cancel fix deletes a large JE once no
    stale queue-submission lock is in the way."""
    import traceback
    from tally_migrator.migration import rollback
    prev = frappe.flags.in_import
    frappe.flags.in_import = True
    frappe.db.savepoint("dbg_del2")
    try:
        doc = frappe.get_doc(doctype, name)
        try:
            doc.unlock()
            print("cleared stale lock")
        except Exception as e:
            print("unlock note:", e)
        reason = rollback._delete_one({"doctype": doctype, "name": name}, set())
        print("reason:", reason if reason else "None (DELETED OK)")
        frappe.db.rollback(save_point="dbg_del2")
        return {"reason": reason}
    except Exception:
        frappe.db.rollback(save_point="dbg_del2")
        traceback.print_exc()
        return {"error": True}
    finally:
        frappe.flags.in_import = prev


def _latest_log() -> str | None:
    rows = frappe.get_all("Tally Migration Log", fields=["name", "status"],
                          order_by="creation desc", limit=1)
    if not rows:
        return None
    print(f"Latest migration log: {rows[0].name}  status={rows[0].status}")
    return rows[0].name


def import_show(log_name: str | None = None) -> dict:
    """Print the live import progress + compact profile (SQL fingerprints, per-record
    timing, per-op split, commit/enqueue counts, RSS) that MasterMigrator streams to the
    tally_migration_progress cache. The import twin of show(); poll it during a run."""
    try:
        log_name = log_name or _latest_log()
        if not log_name:
            print("No Tally Migration Log documents yet.")
            return {}
        data = frappe.cache().get_value(f"tally_migration_progress:{log_name}")
        if not data:
            print(f"No live profile cached for {log_name} "
                  "(run not started, or already expired).")
            return {}
        print(json.dumps(data, indent=2, default=str))
        return data
    except Exception:
        traceback.print_exc()
        return {"error": True}


@frappe.whitelist()
def profile_import(log_name: str = "") -> dict:
    """Diagnostics REST endpoint: the full streamed IMPORT profile from the progress
    cache - percent, description, rss, and the compact profiler snapshot (top SQL
    fingerprints, per-op build/upsert split, commit/enqueue counts). Defaults to the
    latest run. Read-only. Never merged to main."""
    from tally_migrator.api import ALLOWED_ROLES
    frappe.only_for(ALLOWED_ROLES)
    if not log_name:
        rows = frappe.get_all("Tally Migration Log", fields=["name"],
                              order_by="creation desc", limit=1)
        log_name = rows[0].name if rows else ""
    if not log_name:
        return {}
    cached = frappe.cache().get_value(f"tally_migration_progress:{log_name}") or {}
    status = frappe.db.get_value("Tally Migration Log", log_name, "status")
    return {"log": log_name, "status": status, "live": cached}


@frappe.whitelist()
def profile_revert(revert_name: str = "") -> dict:
    """Diagnostics REST endpoint: the live REVERT profile from cache plus the revert
    doc's persisted status/counts. Defaults to the latest revert. Read-only."""
    from tally_migrator.api import ALLOWED_ROLES
    frappe.only_for(ALLOWED_ROLES)
    if not revert_name:
        rows = frappe.get_all("Tally Migration Revert", fields=["name"],
                              order_by="creation desc", limit=1)
        revert_name = rows[0].name if rows else ""
    if not revert_name:
        return {}
    doc = frappe.db.get_value("Tally Migration Revert", revert_name,
                              ["status", "deleted_count", "kept_count"], as_dict=True)
    return {"revert": revert_name, "doc": doc,
            "live": frappe.cache().get_value(f"tally_revert_progress:{revert_name}") or {}}


# ── In-app diagnostics report (session-authenticated, no API key) ────────────────
# The endpoint the log form's "Diagnostics" panel and "Download report" button call.
# It runs under the logged-in session (frappe.call from the desk), so a customer who
# owns the data can view and export the profile themselves without ever handing over
# an API key or the data itself - the default payload is customer-safe.

def _strip_report(report: dict, include_content: bool) -> dict:
    """Return the full per-phase report with each phase's ``slowest`` list reduced to
    timing + record count only. The slowest-record ``content``/``id`` are the *actual
    migrated record* (party name, GSTIN, address...), so they are dropped unless the
    caller explicitly opts in. Everything else (phase timings, SQL *shapes*, memory,
    commit/enqueue/http counts) carries no business data and is always kept."""
    if not isinstance(report, dict):
        return {}
    if include_content:
        return report
    safe = {}
    for label, phase in report.items():
        if not isinstance(phase, dict):
            safe[label] = phase
            continue
        p = dict(phase)
        slow = p.get("slowest")
        if isinstance(slow, list):
            # Keep the timing distribution, drop the record identity/content.
            p["slowest"] = [{"ms": s.get("ms")} for s in slow if isinstance(s, dict)]
        safe[label] = p
    return safe


@frappe.whitelist()
def diagnostics_report(log_name: str = "", include_content: int = 0) -> dict:
    """Session-authenticated diagnostics for a migration run, safe to share.

    Assembles one payload from two sources so it works for both a *finished* run and a
    *crashed / OOM-killed* one:
      - the crash-proof progress cache (live compact profile + memory curve, streamed
        during the run so it survives a worker that never wrote a terminal status), and
      - the log's persisted ``_profile`` / ``_phase_seconds`` (written at finalize).

    ``include_content`` defaults to 0: the returned profile contains phase timings, SQL
    *shapes* (fingerprinted, never literal values), the memory trail, and
    commit/enqueue/http counts - none of which is business data - so a customer can hand
    it over to diagnose where a run stalls without disclosing their records. Pass 1 only
    with the data owner's consent; it adds the slowest records' own field content.

    Read-only, role-guarded, never merged to main. Defaults to the latest run."""
    import json
    from tally_migrator.api import ALLOWED_ROLES
    frappe.only_for(ALLOWED_ROLES)
    include = bool(int(include_content or 0))
    if not log_name:
        rows = frappe.get_all("Tally Migration Log", fields=["name"],
                              order_by="creation desc", limit=1)
        log_name = rows[0].name if rows else ""
    if not log_name:
        return {}
    row = frappe.db.get_value(
        "Tally Migration Log", log_name,
        ["status", "company", "extracted_counts"], as_dict=True) or {}
    cached = frappe.cache().get_value(f"tally_migration_progress:{log_name}") or {}
    # The persisted profile lives inside extracted_counts JSON under two private keys.
    phase_seconds, full_report = {}, {}
    try:
        ec = json.loads(row.get("extracted_counts") or "{}")
        phase_seconds = ec.get("_phase_seconds") or {}
        full_report = ec.get("_profile") or {}
    except Exception:
        pass
    return {
        "log": log_name,
        "status": row.get("status"),
        "company": row.get("company"),
        "generated_at": str(frappe.utils.now()),
        "branch": "fc/e2e-profiler",
        "includes_record_content": include,
        # Live, crash-proof stream (present even if the run never finalised).
        "percent": cached.get("percent"),
        "description": cached.get("description"),
        "rss_mb": cached.get("rss_mb"),
        "peak_mb": cached.get("peak_mb"),
        "mem_trail": cached.get("mem_trail") or [],
        "live_profile": cached.get("profile") or {},
        # Persisted at finalize (richer, per-phase percentiles + top-20 SQL).
        "phase_seconds": phase_seconds,
        "report": _strip_report(full_report, include),
    }
