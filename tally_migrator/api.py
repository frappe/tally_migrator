import json

import frappe

from tally_migrator.tally.config import TallyConfig
from tally_migrator.tally.file_source import FileTallySource
from tally_migrator.tally.extractors import TallyExtractor, ITEM_FIELDS
from tally_migrator.erpnext.uom_resolver import UomResolver
from tally_migrator.validation.engine import (
    validate_extraction, group_report, records_by_key, erpnext_states,
)
from tally_migrator.migration.overrides import apply_record_overrides
from tally_migrator.migration.coverage import coverage_report
from tally_migrator.migration.readiness import check_readiness
from tally_migrator.migration.master_migrator import MasterMigrator

ALLOWED_ROLES = ["System Manager", "Tally Migration Manager"]


@frappe.whitelist()
def preview_masters_file(file_url):
    """Parse an uploaded Tally Masters XML and report what it contains.

    Read-only: imports nothing. Lets the user confirm the file is valid and see
    record counts (customers / suppliers / items / warehouses) *before* running
    the migration, so there are no surprises.
    """
    frappe.only_for(ALLOWED_ROLES)
    _, source = _source_from_file(file_url)
    extractor = TallyExtractor(source)
    return {**extractor.extract_all().summary, **extractor.extract_coa().summary}


@frappe.whitelist()
def validate_masters_file(file_url):
    """Pre-flight check: find UOMs used in the file that don't exist in ERPNext.

    Returns a list of issues (one per unique Tally UOM that maps to a missing
    ERPNext UOM) and the full list of existing ERPNext UOMs so the frontend
    can render a resolution dropdown. Read-only — creates nothing.
    """
    frappe.only_for(ALLOWED_ROLES)
    _, source = _source_from_file(file_url)
    items = source.get_collection("Stock Item", ITEM_FIELDS)
    resolver = UomResolver(
        u["name"] for u in frappe.get_all("UOM", fields=["name"], limit_page_length=0)
    )
    return {
        "issues": resolver.issues_for(r.get("BaseUnits") for r in items),
        "all_uoms": resolver.existing_sorted,
    }


@frappe.whitelist()
def company_readiness(erpnext_company=""):
    """Pre-flight: is the target ERPNext company set up to receive masters?

    Read-only. Returns blockers (an entire entity would fail) and warnings
    (partial degradation) so the UI can stop a doomed run before it starts.
    """
    frappe.only_for(ALLOWED_ROLES)
    return check_readiness(erpnext_company)


@frappe.whitelist()
def validate_masters_data(file_url, record_overrides="", erpnext_company=""):
    """Pre-flight data-quality scan of an uploaded Tally Masters XML.

    Read-only — extracts and inspects, writes nothing. Returns a grouped,
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
    masters = apply_record_overrides(TallyExtractor(source).extract_all(), overrides)
    payload = group_report(validate_extraction(masters=masters), records_by_key(masters))
    payload["states"] = erpnext_states()
    payload["coverage"] = coverage_report(source)
    if erpnext_company:
        payload["readiness"] = check_readiness(erpnext_company)
    return payload


@frappe.whitelist()
def create_uoms(uom_names):
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


@frappe.whitelist()
def run_masters_migration_from_file(file_url, erpnext_company="", uom_overrides="",
                                    validation_report="", record_overrides="", coa_mode="reuse"):
    """Run the masters migration from an uploaded Tally masters XML export.

    ``file_url``        – URL of the File uploaded via the standard Frappe uploader.
    ``erpnext_company`` – target Company inside ERPNext.
    ``uom_overrides``   – JSON object ``{"TallyUOM": "ERPNextUOM", ...}`` resolved
                          by the user in the pre-flight check. Takes precedence over
                          the built-in UOM_MAP for the listed keys.

    The pipeline (Warehouses → Customers → Suppliers → Items) publishes progress
    on the realtime bus and returns a summary dict that includes ``log_name`` so
    the UI can link directly to the migration log.
    """
    frappe.only_for(ALLOWED_ROLES)
    uom: dict = json.loads(uom_overrides) if uom_overrides else {}
    records: dict = json.loads(record_overrides) if record_overrides else {}

    file_doc, source = _source_from_file(file_url)
    config = TallyConfig(
        erpnext_company=erpnext_company,
        tally_company=f"File: {file_doc.file_name or file_url}",
        source_file=file_url,
        validation_report=validation_report or "",
        # Computed server-side from the actual file so the stored audit record of
        # un-migrated fields is authoritative, not client-supplied.
        coverage_report=frappe.as_json(coverage_report(source)),
        coa_mode=coa_mode if coa_mode in ("reuse", "mirror") else "reuse",
    )
    return _run_and_summarize(config, source, uom, records)


@frappe.whitelist()
def rerun_from_log(log_name):
    """Re-run a migration from the source file stored on an existing log.

    The import is idempotent — records that already exist are skipped — so a full
    re-run effectively retries only the records that failed last time (typically
    once their underlying issue, e.g. a missing UOM, has been resolved). A fresh
    log is created so the run history is preserved.
    """
    frappe.only_for(ALLOWED_ROLES)
    log = frappe.get_doc("Tally Migration Log", log_name)
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
    )
    return _run_and_summarize(config, source)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _source_from_file(file_url):
    """Load the uploaded File and wrap it as a FileTallySource.

    Returns ``(file_doc, source)`` — most callers only need the source, but the
    migration run also reads ``file_doc.file_name`` for the log label.
    """
    file_doc = frappe.get_doc("File", {"file_url": file_url})
    return file_doc, FileTallySource(_decode(file_doc.get_content()))


def _run_and_summarize(config: TallyConfig, source, uom_overrides: dict | None = None,
                       record_overrides: dict | None = None) -> dict:
    """Run a masters migration and return its summary dict plus the log name."""
    migrator = MasterMigrator(
        config, source=source,
        uom_overrides=uom_overrides or {},
        record_overrides=record_overrides or {},
    )
    result = migrator.run().as_dict()
    result["log_name"] = migrator.log.name if migrator.log else None
    return result


def _decode(content) -> str:
    """Decode File.get_content() bytes/str to text."""
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return content
