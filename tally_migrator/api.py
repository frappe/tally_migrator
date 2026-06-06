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
def run_masters_migration_from_file(file_url, erpnext_company=""):
    """Run the masters migration from an uploaded Tally masters XML export.

    ``file_url`` is the URL of a File attached via the standard Frappe uploader.
    The pipeline (Warehouses → Customers → Suppliers → Items) publishes progress
    on the realtime bus and returns a summary dict.
    """
    frappe.only_for(["System Manager", "Tally Migration Manager"])
    file_doc = frappe.get_doc("File", {"file_url": file_url})
    xml_text = _decode(file_doc.get_content())
    config = TallyConfig(
        erpnext_company=erpnext_company,
        # The migration log needs a source label; record the export file name.
        tally_company=f"File: {file_doc.file_name or file_url}",
    )
    summary = MasterMigrator(config, source=FileTallySource(xml_text)).run()
    return summary.as_dict()


def _decode(content) -> str:
    """Decode File.get_content() bytes/str to text."""
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return content
