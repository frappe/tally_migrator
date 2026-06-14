"""Fresh-site bootstrap: complete the ERPNext setup wizard programmatically.

``bench install-app erpnext`` installs the app but does NOT run the setup wizard,
so a brand-new site has none of the default masters the integration suite relies
on - Customer Group "Commercial", Territory "All Territories", the root Item /
Supplier groups, base UOMs like "Nos". The importer tests assume a fully set-up
site, which every real migration target is. CI calls this once after installing
ERPNext to reach that same baseline.

Run standalone (this is what CI does):
    bench --site <site> execute tally_migrator.tests.bootstrap.complete_erpnext_setup
"""
import frappe

# A fixed, minimal company. India + INR so the India Compliance GST path (which CI
# also installs) has a real company to attach its settings to, mirroring the
# India Frappe Cloud production target these migrations run against.
_SETUP_ARGS = {
    "country": "India",
    "currency": "INR",
    "timezone": "Asia/Kolkata",
    "language": "en",
    "company_name": "Frappe Tech",
    "company_abbr": "FT",
    "chart_of_accounts": "Standard",
    "domain": "Distribution",
    "fy_start_date": "2025-04-01",
    "fy_end_date": "2026-03-31",
}


def complete_erpnext_setup():
    """Run ERPNext's setup_complete once. Idempotent: a site that already has a
    Company has been set up, so re-running is a no-op (safe to call on every CI run
    and on a re-used site)."""
    if frappe.get_all("Company", limit=1):
        print("ERPNext setup already complete (a Company exists) - skipping")
        return

    from erpnext.setup.setup_wizard.setup_wizard import setup_complete

    # setup_company() reads args via attribute access (args.fy_start_date), so the
    # args must be a frappe._dict, not a plain dict - hence this helper rather than
    # calling setup_complete straight from `bench execute` with JSON.
    setup_complete(frappe._dict(_SETUP_ARGS))
    frappe.db.commit()
    print("ERPNext setup complete: company '{0}' and default masters created".format(
        _SETUP_ARGS["company_name"]))
