import json
import threading
from collections import OrderedDict

import frappe

from tally_migrator.tally.config import TallyConfig
from tally_migrator.tally.file_source import FileTallySource, decode_tally_bytes
from tally_migrator.tally.extractors import TallyExtractor, ITEM_FIELDS, ITEM_TAGS
from tally_migrator.erpnext.uom_resolver import UomResolver
from tally_migrator.validation.engine import (
    validate_extraction, group_report, records_by_key, erpnext_states,
)
from tally_migrator.migration.overrides import apply_record_overrides
from tally_migrator.migration.coverage import coverage_report
from tally_migrator.migration.account_mapping import account_mapping
from tally_migrator.migration.readiness import check_readiness
from tally_migrator.migration.master_migrator import MasterMigrator

ALLOWED_ROLES = ["System Manager", "Tally Migration Manager"]


@frappe.whitelist(methods=["GET", "POST"])
def preview_masters_file(file_url: str):
    """Parse an uploaded Tally Masters XML and report what it contains.

    Read-only: imports nothing. Lets the user confirm the file is valid and see
    record counts (customers / suppliers / items / warehouses) *before* running
    the migration, so there are no surprises.
    """
    frappe.only_for(ALLOWED_ROLES)
    _, source = _source_from_file(file_url)
    extractor = TallyExtractor(source)
    return {**extractor.extract_all().summary, **extractor.extract_coa().summary}


@frappe.whitelist(methods=["GET", "POST"])
def validate_masters_file(file_url: str):
    """Pre-flight check: find UOMs used in the file that don't exist in ERPNext.

    Returns a list of issues (one per unique Tally UOM that maps to a missing
    ERPNext UOM) and the full list of existing ERPNext UOMs so the frontend
    can render a resolution dropdown. Read-only - creates nothing.
    """
    frappe.only_for(ALLOWED_ROLES)
    _, source = _source_from_file(file_url)
    items = source.get_collection("Stock Item", ITEM_FIELDS, ITEM_TAGS)
    resolver = UomResolver(
        u["name"] for u in frappe.get_all("UOM", fields=["name"], limit_page_length=0)
    )
    return {
        "issues": resolver.issues_for(r.get("BaseUnits") for r in items),
        "all_uoms": resolver.existing_sorted,
    }


@frappe.whitelist(methods=["GET", "POST"])
def get_companies():
    """List ERPNext companies for the wizard's target-company picker.

    The wizard runs under the Tally Migration Manager role, which deliberately
    holds no Company read permission (it only needs to migrate, not browse the
    Company master). A plain ``frappe.client.get_list`` would therefore return
    nothing. This gated endpoint returns the names with ``ignore_permissions`` so
    the picker works without widening the role.
    """
    frappe.only_for(ALLOWED_ROLES)
    return frappe.get_all(
        "Company", fields=["name"], order_by="name", ignore_permissions=True
    )


@frappe.whitelist(methods=["GET", "POST"])
def company_readiness(erpnext_company: str = "", posting_date: str = ""):
    """Pre-flight: is the target ERPNext company set up to receive masters?

    Read-only. Returns blockers (an entire entity would fail) and warnings
    (partial degradation) so the UI can stop a doomed run before it starts.
    ``posting_date`` (optional) is checked against frozen periods / fiscal years.
    """
    frappe.only_for(ALLOWED_ROLES)
    return check_readiness(erpnext_company, posting_date)


@frappe.whitelist(methods=["GET", "POST"])
def validate_masters_data(file_url: str, record_overrides: str = "", erpnext_company: str = "",
                          posting_date: str = ""):
    """Pre-flight data-quality scan of an uploaded Tally Masters XML.

    Read-only - extracts and inspects, writes nothing. Returns a grouped,
    UI-ready report (issues collapsed by rule code, errors first) plus the inline
    editor metadata (editable fields + current values + the state list) so the user
    can fix flagged fields and decide (fix / proceed anyway) before any migration.

    ``record_overrides`` is the JSON of edits made on the screen so far; they are
    applied in memory before re-validating, so "Re-check" confirms fixes against
    the same rules. The uploaded file itself is never modified.
    """
    frappe.only_for(ALLOWED_ROLES)
    overrides = json.loads(record_overrides) if record_overrides else {}
    _, source = _source_from_file(file_url)
    extractor = TallyExtractor(source)
    masters = apply_record_overrides(extractor.extract_all(), overrides)
    # COA is extracted too so hierarchy checks (cycles) can cover accounts and cost
    # centres, not just the inventory masters carried on ``masters``.
    coa = extractor.extract_coa()
    payload = group_report(
        validate_extraction(masters=masters, coa=coa), records_by_key(masters))
    payload["states"] = erpnext_states()
    payload["coverage"] = coverage_report(source)
    payload["account_mapping"] = account_mapping(source)
    if erpnext_company:
        payload["readiness"] = check_readiness(erpnext_company, posting_date)
    return payload


@frappe.whitelist(methods=["POST"])
def create_uoms(uom_names: str):
    """Batch-create UOM records that don't already exist.

    Called from the pre-flight check screen when the user opts to create one or
    more missing Units of Measure. One round-trip for the whole batch (scales to
    hundreds of units), instead of one insert per row from the browser.

    ``uom_names`` is a JSON list of names. Returns
    ``{created: [...], existing: [...], failed: {name: reason}}``.
    """
    frappe.only_for(ALLOWED_ROLES)
    names = json.loads(uom_names) if isinstance(uom_names, str) else (uom_names or [])

    created, existing, failed = [], [], {}
    for raw in names:
        name = (raw or "").strip()
        if not name:
            continue
        if frappe.db.exists("UOM", name):
            existing.append(name)
            continue
        try:
            doc = frappe.new_doc("UOM")
            doc.uom_name = name
            doc.insert(ignore_permissions=True)
            created.append(name)
        except Exception as exc:
            failed[name] = str(exc)
    frappe.db.commit()
    return {"created": created, "existing": existing, "failed": failed}


# ── Wizard draft (resume an in-progress migration after reload/logout) ──────────

_DRAFT_DOCTYPE = "Tally Migration Draft"


@frappe.whitelist(methods=["POST"])
def save_draft(payload: str):
    """Upsert the current user's in-progress wizard state (one draft per user).

    The wizard autosaves here after every inline fix / step change so an accidental
    reload or logout doesn't lose the user's work. Stores only references + the
    user's own edits (file URL, company, options, UOM + record overrides, step).
    """
    frappe.only_for(ALLOWED_ROLES)
    data = json.loads(payload) if isinstance(payload, str) else (payload or {})
    if not data.get("file_url"):
        return {"saved": False}        # nothing meaningful to persist yet

    user = frappe.session.user
    name = frappe.db.exists(_DRAFT_DOCTYPE, user)
    doc = (frappe.get_doc(_DRAFT_DOCTYPE, name) if name
           else frappe.new_doc(_DRAFT_DOCTYPE))
    doc.user = user
    doc.file_url = data.get("file_url") or ""
    doc.file_name = data.get("file_name") or ""
    doc.erpnext_company = data.get("erpnext_company") or ""
    doc.coa_mode = data.get("coa_mode") or ""
    doc.posting_date = data.get("posting_date") or ""
    doc.step = data.get("step") or ""
    doc.uom_overrides = frappe.as_json(data.get("uom_overrides") or {})
    doc.record_overrides = frappe.as_json(data.get("record_overrides") or {})
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return {"saved": True}


@frappe.whitelist(methods=["GET", "POST"])
def get_draft():
    """Return the current user's saved wizard draft, or ``None`` if there is none."""
    frappe.only_for(ALLOWED_ROLES)
    name = frappe.db.exists(_DRAFT_DOCTYPE, frappe.session.user)
    if not name:
        return None
    d = frappe.get_doc(_DRAFT_DOCTYPE, name)
    # Only offer a resume if the uploaded file still exists - a draft pointing at a
    # deleted File is stale and would just fail on resume.
    if not d.file_url or not frappe.db.exists("File", {"file_url": d.file_url}):
        return None
    return {
        "file_url": d.file_url,
        "file_name": d.file_name,
        "erpnext_company": d.erpnext_company,
        "coa_mode": d.coa_mode,
        "posting_date": d.posting_date,
        "step": d.step,
        "uom_overrides": json.loads(d.uom_overrides or "{}"),
        "record_overrides": json.loads(d.record_overrides or "{}"),
        "modified": str(d.modified),
    }


@frappe.whitelist(methods=["POST"])
def clear_draft():
    """Delete the current user's wizard draft (on 'start over' or after a run)."""
    frappe.only_for(ALLOWED_ROLES)
    name = frappe.db.exists(_DRAFT_DOCTYPE, frappe.session.user)
    if name:
        frappe.delete_doc(_DRAFT_DOCTYPE, name, ignore_permissions=True)
        frappe.db.commit()
    return {"cleared": True}


@frappe.whitelist(methods=["POST"])
def run_masters_migration_from_file(file_url: str, erpnext_company: str = "", uom_overrides: str = "",
                                    validation_report: str = "", record_overrides: str = "",
                                    coa_mode: str = "reuse", posting_date: str = "",
                                    created_uoms: str = ""):
    """Run the masters migration from an uploaded Tally masters XML export.

    ``file_url``        - URL of the File uploaded via the standard Frappe uploader.
    ``erpnext_company`` - target Company inside ERPNext.
    ``uom_overrides``   - JSON object ``{"TallyUOM": "ERPNextUOM", ...}`` resolved
                          by the user in the pre-flight check. Takes precedence over
                          the built-in UOM_MAP for the listed keys.

    The pipeline (Warehouses → Customers → Suppliers → Items) publishes progress
    on the realtime bus and returns a summary dict that includes ``log_name`` so
    the UI can link directly to the migration log.
    """
    frappe.only_for(ALLOWED_ROLES)
    _assert_no_active_run(erpnext_company)
    uom: dict = json.loads(uom_overrides) if uom_overrides else {}
    records: dict = json.loads(record_overrides) if record_overrides else {}
    created: list = json.loads(created_uoms) if created_uoms else []

    file_doc, source = _source_from_file(file_url)
    config = _build_masters_config(
        file_url, file_doc.file_name, erpnext_company, source,
        validation_report, coa_mode, posting_date)

    # Large imports can run longer than the web request's proxy/gunicorn timeout,
    # so above a record threshold we create the log up front and hand the run to a
    # background worker, returning immediately. The page then tracks the log to
    # completion (progress still streams over the realtime bus). Smaller imports
    # stay synchronous and return the full summary in one round-trip.
    if source.approx_record_count() > RUN_ASYNC_THRESHOLD:
        return _enqueue_masters_run(
            config, file_url, erpnext_company, uom_overrides or "",
            validation_report or "", record_overrides or "", coa_mode, posting_date or "",
            created_uoms or "")
    return _run_and_summarize(config, source, uom, records, created)


@frappe.whitelist(methods=["POST"])
def rerun_from_log(log_name: str):
    """Re-run a migration from the source file stored on an existing log.

    The import is idempotent - records that already exist are skipped - so a full
    re-run effectively retries only the records that failed last time (typically
    once their underlying issue, e.g. a missing UOM, has been resolved). A fresh
    log is created so the run history is preserved.
    """
    frappe.only_for(ALLOWED_ROLES)
    log = frappe.get_doc("Tally Migration Log", log_name)
    _assert_no_active_run(log.company)
    if not log.source_file:
        frappe.throw(
            "This log has no source file stored, so it can't be re-run automatically. "
            "Open the Tally Migrator page and upload the file again."
        )

    _, source = _source_from_file(log.source_file)
    config = TallyConfig(
        erpnext_company=log.company,
        tally_company=log.tally_company,
        source_file=log.source_file,
        validation_report=log.validation_report or "",
        # Recomputed from the (unchanged) source so the new log's coverage is current.
        coverage_report=frappe.as_json(coverage_report(source)),
        mapping_report=frappe.as_json(account_mapping(source)),
        # Repeat the original run's options rather than silently reverting to
        # defaults (reuse / fiscal-year start).
        coa_mode=log.coa_mode or "reuse",
        posting_date=str(log.posting_date or ""),
    )
    # Replay the user's original pre-flight choices, or the re-run silently reverts
    # custom UOMs to defaults and drops every inline GST/state/HSN fix that made the
    # first run viable.
    uom = json.loads(log.uom_overrides) if log.get("uom_overrides") else {}
    records = json.loads(log.record_overrides) if log.get("record_overrides") else {}
    return _run_and_summarize(config, source, uom, records)


# ── Internal helpers ──────────────────────────────────────────────────────────

# LRU of recently-parsed sources, keyed by (user, File name, modified timestamp). The
# wizard re-calls validate/preview on every inline fix ("Re-check"), each of which
# would otherwise re-read, re-decode, re-sanitize and re-parse the whole file. The
# File's bytes are immutable for a given (name, modified), so a cached parse is
# always valid; a new upload (new name) or an edit (new modified) misses and
# re-parses. The key includes the user so one person's upload can never hand another
# person's request a different file's parse.
#
# Bounded to ``_SOURCE_CACHE_MAX`` entries (least-recently-used evicted): a single
# slot meant two managers migrating at once - or one user re-checking two files -
# kept evicting each other and re-parsing a large file on every call. A handful of
# entries removes that thrash; the cap still limits how much parsed data can pin
# memory (worst case ~_SOURCE_CACHE_MAX large files), so it stays a small number.
#
# The cache is a process global shared by every worker thread, so all access goes
# through ``_SOURCE_CACHE_LOCK``: without it two concurrent requests could mutate the
# ordering / evict mid-read.
_SOURCE_CACHE: "OrderedDict" = OrderedDict()
_SOURCE_CACHE_LOCK = threading.Lock()
_SOURCE_CACHE_MAX = 4

# Reject uploads above this size before parsing, with an actionable message,
# rather than letting a multi-gigabyte file exhaust the worker. UTF-16 exports
# decode to roughly half this many characters. Overridable via site config
# (``tally_migrator_max_upload_mb``) for operators who need a higher ceiling.
_DEFAULT_MAX_UPLOAD_MB = 150

# Above this many master records a run is moved to a background job instead of
# blocking the web request until it finishes (which would hit the proxy/gunicorn
# timeout). Below it, the run stays synchronous and returns the summary directly.
RUN_ASYNC_THRESHOLD = 5000

# A 'Running' log older than this is treated as stale (its worker likely died
# without finalising), so it no longer blocks a fresh run - otherwise a crashed run
# would lock the company out forever. The per-company opening lock (importers) is
# the real double-post guard; this guard is an early, clearer message for the
# common two-tab / double-click mistake.
_ACTIVE_RUN_STALE_SECONDS = 2 * 60 * 60


def _is_job_alive(job_id: str) -> bool:
    """Is the RQ job for an enqueued run still queued or running?

    Wraps Frappe's ``is_job_enqueued`` defensively: if RQ/Redis can't be reached we
    return True (assume alive) so a transient lookup failure can't wave through a
    genuine concurrent run - the duplicate-start guard fails closed."""
    try:
        from frappe.utils.background_jobs import is_job_enqueued
        return is_job_enqueued(job_id)
    except Exception:
        return True


def _assert_no_active_run(company: str) -> None:
    """Refuse to start a second migration while one is already running for the
    same company. A stale run (crashed worker) is ignored so re-runs aren't locked
    out. For an async run we ask RQ whether the worker is genuinely still alive; for
    a sync run (no job id, request-bound) we fall back to an age cap. Best-effort UX
    guard - the opening lock in the importer is what actually protects the books."""
    if not company:
        return
    rows = frappe.get_all(
        "Tally Migration Log",
        filters={"company": company, "status": "Running"},
        fields=["name", "modified", "job_id"], order_by="modified desc", limit=1,
    )
    if not rows:
        return
    modified = rows[0].modified
    job_id = rows[0].get("job_id")
    if job_id:
        # Liveness, not age: an enqueued run blocks only while its RQ job is actually
        # queued or running. A crashed/finished/missing job leaves a 'Running' log
        # behind, but is_job_enqueued() returns False, so it never locks out a re-run -
        # and a legitimately long (>2h) job keeps blocking, which the age cap couldn't.
        if not _is_job_alive(job_id):
            return
    elif frappe.utils.time_diff_in_seconds(frappe.utils.now(), modified) >= _ACTIVE_RUN_STALE_SECONDS:
        return
    frappe.throw(
        frappe._(
            "A migration for '{0}' is already running (started {1}). Please wait "
            "for it to finish, or open the migration log to check its status."
        ).format(company, frappe.utils.pretty_date(modified))
    )


def _assert_file_access(file_doc) -> None:
    """Refuse to read a File the current user has no claim to.

    The whitelisted handlers accept an arbitrary ``file_url``; without this a
    Tally Migration Manager could pass another user's File URL (the wizard's own
    uploads are private, but a manager could also point at any *public* File on the
    site) and have the server read its bytes - an IDOR. The migrator only ever needs
    the file the current user uploaded, which they own, so access is limited to the
    owner (plus System Manager / Administrator for support and cross-user re-runs).
    """
    user = frappe.session.user
    if user == "Administrator" or file_doc.owner == user:
        return
    if "System Manager" in frappe.get_roles(user):
        return
    frappe.throw(
        frappe._("You are not permitted to access this file."),
        frappe.PermissionError,
    )


def _source_from_file(file_url):
    """Load the uploaded File and wrap it as a FileTallySource.

    Returns ``(file_doc, source)`` - most callers only need the source, but the
    migration run also reads ``file_doc.file_name`` for the log label. Access is
    checked (see ``_assert_file_access``) and the parse is cached per file version.
    """
    # Frappe stores byte-identical uploads at one path, so a single file_url can map
    # to several File rows owned by different users (e.g. the same export uploaded by
    # two people). Prefer the row owned by the current user; otherwise fall back to any
    # match (access is still enforced by _assert_file_access below).
    name = frappe.db.exists(
        "File", {"file_url": file_url, "owner": frappe.session.user}
    ) or frappe.db.exists("File", {"file_url": file_url})
    # A stale draft (or a re-run from a log) can point at a File that has since been
    # deleted; surface that as a clear, actionable message instead of a raw 500.
    if not name:
        frappe.throw(
            frappe._(
                "The uploaded file could not be found - it may have been deleted. "
                "Please upload your Tally Masters XML again."
            ),
            frappe.DoesNotExistError,
        )
    file_doc = frappe.get_doc("File", name)
    _assert_file_access(file_doc)
    cache_key = (frappe.session.user, file_doc.name, str(file_doc.modified))
    with _SOURCE_CACHE_LOCK:
        source = _SOURCE_CACHE.get(cache_key)
        if source is not None:
            _SOURCE_CACHE.move_to_end(cache_key)     # mark most-recently used
            return file_doc, source
        source = FileTallySource(_decode(_raw_file_bytes(file_doc)))
        _SOURCE_CACHE[cache_key] = source            # inserts as most-recently used
        while len(_SOURCE_CACHE) > _SOURCE_CACHE_MAX:
            _SOURCE_CACHE.popitem(last=False)        # evict least-recently used
    return file_doc, source


def _raw_file_bytes(file_doc) -> bytes:
    """Return the uploaded file's raw bytes.

    ``File.get_content()`` can hand back an already-decoded ``str`` (recent Frappe
    decodes text uploads as UTF-8 with replacement). That destroys the byte-order
    mark on a genuine UTF-16 Tally export, so our own encoding detection in
    ``decode_tally_bytes`` never runs and the parser dies at byte 0. Reading binary
    keeps the real bytes - and the BOM - intact.
    """
    content = file_doc.get_content()
    raw = bytes(content) if isinstance(content, (bytes, bytearray)) else None
    if raw is not None:
        _assert_within_size_limit(len(raw))
        return raw
    # A str means get_content() already decoded (and likely corrupted) the bytes;
    # re-read the original from disk so UTF-16 survives.
    try:
        with open(file_doc.get_full_path(), "rb") as fh:
            raw = fh.read()
        _assert_within_size_limit(len(raw))
        return raw
    except frappe.ValidationError:
        raise                       # the size-limit rejection must propagate
    except Exception:
        frappe.log_error(
            title="tally_migrator: could not read original file bytes",
            message=frappe.get_traceback(),
        )
        # The disk re-read failed, so all we have is the already-decoded str. Re-encoding
        # it as latin-1 is lossless only if the original was a latin-1 round-trip; for a
        # genuine UTF-16 Tally export it silently corrupts the data and the migration
        # then imports wrong masters with no error. Fail loud by default so the user
        # never gets silently-wrong books. Operators who knowingly handle latin-1 exports
        # can opt back into the legacy best-effort path via site config.
        if frappe.conf.get("tally_migrator_strict_decode", True):
            frappe.throw(
                frappe._(
                    "Could not read the original bytes of this file, so it can't be "
                    "imported safely. Please upload your Tally Masters XML again."
                )
            )
        encoded = content.encode("latin-1", errors="ignore")
        _assert_within_size_limit(len(encoded))
        return encoded


def _assert_within_size_limit(num_bytes: int) -> None:
    """Reject an oversized upload before parsing, with an actionable message."""
    max_mb = frappe.conf.get("tally_migrator_max_upload_mb") or _DEFAULT_MAX_UPLOAD_MB
    if num_bytes > max_mb * 1024 * 1024:
        frappe.throw(
            frappe._(
                "This file is {0} MB, above the {1} MB limit for a single import. "
                "Export your Tally masters in smaller batches (for example a few "
                "ledger groups at a time) and import them one after another - the "
                "migration is idempotent, so already-imported records are skipped."
            ).format(round(num_bytes / (1024 * 1024), 1), max_mb)
        )


def _build_masters_config(file_url, file_name, erpnext_company, source,
                          validation_report, coa_mode, posting_date) -> TallyConfig:
    """Assemble the TallyConfig for a masters run (shared by sync + background)."""
    return TallyConfig(
        erpnext_company=erpnext_company,
        tally_company=f"File: {file_name or file_url}",
        source_file=file_url,
        validation_report=validation_report or "",
        # Computed server-side from the actual file so the stored audit record of
        # un-migrated fields is authoritative, not client-supplied.
        coverage_report=frappe.as_json(coverage_report(source)),
        mapping_report=frappe.as_json(account_mapping(source)),
        coa_mode=coa_mode if coa_mode in ("reuse", "mirror") else "reuse",
        posting_date=posting_date or "",
    )


def _run_and_summarize(config: TallyConfig, source, uom_overrides: dict | None = None,
                       record_overrides: dict | None = None,
                       created_uoms: list | None = None) -> dict:
    """Run a masters migration and return its summary dict plus the log name."""
    migrator = MasterMigrator(
        config, source=source,
        uom_overrides=uom_overrides or {},
        record_overrides=record_overrides or {},
        created_uoms=created_uoms or [],
    )
    result = migrator.run().as_dict()
    result["log_name"] = migrator.log.name if migrator.log else None
    return result


def _enqueue_masters_run(config: TallyConfig, file_url, erpnext_company, uom_overrides,
                         validation_report, record_overrides, coa_mode, posting_date,
                         created_uoms="") -> dict:
    """Create the log now, hand the run to a background worker, return the log name.

    The log is created (and committed) in the request so the page has something to
    track immediately; the worker reuses that same log rather than creating a new
    one. ``enqueue_after_commit`` ensures the job is only published once the log is
    durably committed, so the worker can never race ahead of it.
    """
    migrator = MasterMigrator(config, source=None)
    log = migrator._create_log()
    # Stamp the log with the RQ job id so the active-run guard can ask RQ whether the
    # worker is genuinely still alive (vs crashed), instead of relying only on age.
    job_id = f"tally-masters-{log.name}"
    log.db_set("job_id", job_id, commit=True)
    frappe.enqueue(
        "tally_migrator.api._run_masters_job",
        queue="long",
        timeout=4 * 60 * 60,
        enqueue_after_commit=True,
        job_id=job_id,
        file_url=file_url,
        erpnext_company=erpnext_company,
        uom_overrides=uom_overrides,
        validation_report=validation_report,
        record_overrides=record_overrides,
        coa_mode=coa_mode,
        posting_date=posting_date,
        created_uoms=created_uoms,
        log_name=log.name,
    )
    return {"enqueued": True, "log_name": log.name, "company": erpnext_company}


def _run_masters_job(file_url, erpnext_company, uom_overrides, validation_report,
                     record_overrides, coa_mode, posting_date, log_name, created_uoms=""):
    """Background entry point: re-parse the file and run the migration into an
    already-created log. Errors are recorded on the log by ``MasterMigrator`` and
    re-raised so the failure is also visible in the job/error log."""
    uom = json.loads(uom_overrides) if uom_overrides else {}
    records = json.loads(record_overrides) if record_overrides else {}
    created = json.loads(created_uoms) if created_uoms else []
    file_doc, source = _source_from_file(file_url)
    config = _build_masters_config(
        file_url, file_doc.file_name, erpnext_company, source,
        validation_report, coa_mode, posting_date)
    log = frappe.get_doc("Tally Migration Log", log_name)
    MasterMigrator(
        config, source=source, uom_overrides=uom, record_overrides=records, log=log,
        created_uoms=created,
    ).run()


def _decode(content) -> str:
    """Decode File.get_content() bytes/str to text.

    Real Tally exports are UTF-16; ``decode_tally_bytes`` detects the BOM so they
    don't arrive as mojibake (and fail to parse)."""
    return decode_tally_bytes(content)
