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
def run_masters_migration_from_file(file_url, erpnext_company="", uom_overrides=""):
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
    )
    migrator = MasterMigrator(config, source=FileTallySource(xml_text), uom_overrides=overrides)
    summary = migrator.run()
    result = summary.as_dict()
    result["log_name"] = migrator.log.name if migrator.log else None
    return result


def _decode(content) -> str:
    """Decode File.get_content() bytes/str to text."""
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return content
