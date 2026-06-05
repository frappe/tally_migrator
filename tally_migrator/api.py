import frappe

from tally_migrator.tally.client import TallyClient, TallyConfig
from tally_migrator.migration.master_migrator import MasterMigrator


def _make_config(
    tally_host: str = "localhost",
    tally_port: int = 9000,
    tally_company: str = "",
    erpnext_company: str = "",
) -> TallyConfig:
    return TallyConfig(
        host=tally_host,
        port=int(tally_port),
        tally_company=tally_company,
        erpnext_company=erpnext_company,
    )


@frappe.whitelist()
def ping_tally(tally_host="localhost", tally_port=9000):
    """Return Tally connectivity status and the list of loaded companies."""
    client = TallyClient(_make_config(tally_host, tally_port))
    reachable = client.ping()
    companies = client.get_companies() if reachable else []
    return {"reachable": reachable, "companies": companies}


@frappe.whitelist()
def debug_company_xml(tally_host="localhost", tally_port=9000):
    """Diagnostic: return the raw XML Tally sends for the company list.

    Temporary — used to inspect what a specific Tally Prime build emits when
    companies aren't being detected. Safe (read-only).
    """
    frappe.only_for(["System Manager", "Tally Migration Manager"])
    client = TallyClient(_make_config(tally_host, tally_port))
    return {"raw": client.raw_companies(), "parsed": client.get_companies()}


@frappe.whitelist()
def run_masters_migration(
    tally_host="localhost",
    tally_port=9000,
    tally_company="",
    erpnext_company="",
):
    """
    Run the Phase 1 masters migration (Warehouses → Customers → Suppliers → Items).

    Publishes progress on the Frappe realtime bus and returns a summary dict.
    """
    frappe.only_for(["System Manager", "Tally Migration Manager"])
    config = _make_config(tally_host, tally_port, tally_company, erpnext_company)
    summary = MasterMigrator(config).run()
    return summary.as_dict()
