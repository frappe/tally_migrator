import json

import frappe

from tally_migrator.tally.config import TallyConfig
from tally_migrator.tally.file_source import FileTallySource
from tally_migrator.migration.master_migrator import MasterMigrator


@frappe.whitelist()
def preview_masters_file(file_url):
    """Parse an uploaded Tally Masters XML and report what it contains.

    Read-only: imports nothing. Lets the user confirm the file is valid and see
    record counts (customers / suppliers / items / warehouses) *before* running
    the migration, so there are no surprises.
    """
    frappe.only_for(["System Manager", "Tally Migration Manager"])
    from tally_migrator.tally.extractors import TallyExtractor

    file_doc = frappe.get_doc("File", {"file_url": file_url})
    xml_text = _decode(file_doc.get_content())
    masters = TallyExtractor(FileTallySource(xml_text)).extract_all()
    return masters.summary


@frappe.whitelist()
def validate_masters_file(file_url):
    """Pre-flight check: find UOMs used in the file that don't exist in ERPNext.

    Returns a list of issues (one per unique Tally UOM that maps to a missing
    ERPNext UOM) and the full list of existing ERPNext UOMs so the frontend
    can render a resolution dropdown. Read-only — creates nothing.
    """
    frappe.only_for(["System Manager", "Tally Migration Manager"])
    from tally_migrator.tally.extractors import ITEM_FIELDS
    from tally_migrator.tally.mappings import UOM_MAP

    file_doc = frappe.get_doc("File", {"file_url": file_url})
    xml_text = _decode(file_doc.get_content())
    source = FileTallySource(xml_text)
    items = source.get_collection("Stock Item", ITEM_FIELDS)

    # Unique Tally UOMs present in the file
    tally_uoms = sorted({
        (r.get("BaseUnits") or "").strip()
        for r in items
        if (r.get("BaseUnits") or "").strip()
    })

    # All UOM names that currently exist in this ERPNext instance
    existing_uoms = {
        u["name"]
        for u in frappe.get_all("UOM", fields=["name"], limit_page_length=500)
    }

    issues = []
    for tally_uom in tally_uoms:
        mapped = UOM_MAP.get(tally_uom, tally_uom)   # fallback: use Tally name as-is
        issues.append({
            "tally_uom": tally_uom,
            "erpnext_uom": mapped,          # what we'd use after mapping
            "exists": mapped in existing_uoms,
        })

    return {
        "issues": issues,
        "all_uoms": sorted(existing_uoms),  # for the "map to existing" dropdown
    }


@frappe.whitelist()
def validate_masters_data(file_url):
    """Pre-flight data-quality scan of an uploaded Tally Masters XML.

    Read-only — extracts and inspects, writes nothing. Returns a grouped,
    UI-ready report (issues collapsed by rule code, errors first) so the user can
    see how dirty the data is and decide (fix in Tally / proceed anyway) before any
    migration runs.
    """
    frappe.only_for(["System Manager", "Tally Migration Manager"])
    from tally_migrator.tally.extractors import TallyExtractor
    from tally_migrator.validation.engine import validate_extraction, group_report

    file_doc = frappe.get_doc("File", {"file_url": file_url})
    xml_text = _decode(file_doc.get_content())
    masters = TallyExtractor(FileTallySource(xml_text)).extract_all()
    report = validate_extraction(masters=masters)
    return group_report(report)


@frappe.whitelist()
def create_uoms(uom_names):
    """Batch-create UOM records that don't already exist.

    Called from the pre-flight check screen when the user opts to create one or
    more missing Units of Measure. One round-trip for the whole batch (scales to
    hundreds of units), instead of one insert per row from the browser.

    ``uom_names`` is a JSON list of names. Returns
    ``{created: [...], existing: [...], failed: {name: reason}}``.
    """
    frappe.only_for(["System Manager", "Tally Migration Manager"])
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
def run_masters_migration_from_file(file_url, erpnext_company="", uom_overrides="", validation_report=""):
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
    frappe.only_for(["System Manager", "Tally Migration Manager"])
    overrides: dict = json.loads(uom_overrides) if uom_overrides else {}

    file_doc = frappe.get_doc("File", {"file_url": file_url})
    xml_text = _decode(file_doc.get_content())
    config = TallyConfig(
        erpnext_company=erpnext_company,
        tally_company=f"File: {file_doc.file_name or file_url}",
        source_file=file_url,
        validation_report=validation_report or "",
    )
    migrator = MasterMigrator(config, source=FileTallySource(xml_text), uom_overrides=overrides)
    summary = migrator.run()
    result = summary.as_dict()
    result["log_name"] = migrator.log.name if migrator.log else None
    return result


@frappe.whitelist()
def rerun_from_log(log_name):
    """Re-run a migration from the source file stored on an existing log.

    The import is idempotent — records that already exist are skipped — so a full
    re-run effectively retries only the records that failed last time (typically
    once their underlying issue, e.g. a missing UOM, has been resolved). A fresh
    log is created so the run history is preserved.
    """
    frappe.only_for(["System Manager", "Tally Migration Manager"])
    log = frappe.get_doc("Tally Migration Log", log_name)
    if not log.source_file:
        frappe.throw(
            "This log has no source file stored, so it can't be re-run automatically. "
            "Open the Tally Migrator page and upload the file again."
        )

    file_doc = frappe.get_doc("File", {"file_url": log.source_file})
    xml_text = _decode(file_doc.get_content())
    config = TallyConfig(
        erpnext_company=log.company,
        tally_company=log.tally_company,
        source_file=log.source_file,
    )
    migrator = MasterMigrator(config, source=FileTallySource(xml_text))
    summary = migrator.run()
    result = summary.as_dict()
    result["log_name"] = migrator.log.name if migrator.log else None
    return result


def _decode(content) -> str:
    """Decode File.get_content() bytes/str to text."""
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return content
