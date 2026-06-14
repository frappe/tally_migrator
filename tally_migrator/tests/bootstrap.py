"""Fresh-site bootstrap: complete the ERPNext setup wizard programmatically.

``bench install-app erpnext`` installs the app but does NOT run the setup wizard,
so a brand-new site has none of the default masters the integration suite relies
on - Customer Group "Commercial", Territory "All Territories", the root Item /
Supplier groups, base UOMs like "Nos". The importer tests assume a fully set-up
site, which every real migration target is. CI calls this once after installing
ERPNext (and the app under test) to reach that same baseline.

Run standalone (this is what CI does):
    bench --site <site> execute tally_migrator.tests.bootstrap.complete_erpnext_setup
"""
import datetime

import frappe

# A fixed, minimal company. India + INR so the India Compliance GST path (which CI
# also installs) has a real company to attach its settings to, mirroring the
# India Frappe Cloud production target these migrations run against.
_COMPANY_ARGS = {
    "country": "India",
    "currency": "INR",
    "timezone": "Asia/Kolkata",
    "language": "en",
    "company_name": "Frappe Tech",
    "company_abbr": "FT",
    "chart_of_accounts": "Standard",
    "domain": "Distribution",
    # The opening-entry integration tests post on 2026-04-01; give the company that
    # fiscal year as its default. _ensure_fiscal_years widens the range below.
    "fy_start_date": "2026-04-01",
    "fy_end_date": "2027-03-31",
}

# Fiscal years to guarantee, as April-March start years. The integration tests post
# opening entries on fixed dates (2024-04-01 and 2026-04-01); an opening entry whose
# posting date has no active Fiscal Year is rejected ("not in any active Fiscal Year").
# ERPNext only creates the company's own FY, so we create a fixed span that covers
# every date the suite uses (deterministic - not tied to the CI runner's clock).
_FISCAL_YEAR_START_YEARS = range(2023, 2028)  # FY 2023-24 .. 2027-28


def _ensure_fiscal_years():
    for start_year in _FISCAL_YEAR_START_YEARS:
        name = "{0}-{1}".format(start_year, start_year + 1)
        if frappe.db.exists("Fiscal Year", name):
            continue
        try:
            frappe.get_doc({
                "doctype": "Fiscal Year",
                "year": name,
                "year_start_date": datetime.date(start_year, 4, 1),
                "year_end_date": datetime.date(start_year + 1, 3, 31),
            }).insert(ignore_permissions=True)
        except Exception:
            # A clash with an existing/overlapping FY is fine - we only need coverage.
            pass
    frappe.db.commit()


def complete_erpnext_setup():
    """Run ERPNext's setup_complete once, then guarantee the fiscal-year span.
    Idempotent: a site that already has a Company has been set up, so the wizard is
    skipped, but the fiscal years are still ensured (safe to call on every CI run)."""
    if frappe.get_all("Company", limit=1):
        print("ERPNext setup already complete (a Company exists) - ensuring fiscal years")
        _ensure_fiscal_years()
        return

    from erpnext.setup.setup_wizard.setup_wizard import setup_complete

    # setup_company() reads args via attribute access (args.fy_start_date), so the
    # args must be a frappe._dict, not a plain dict - hence this helper rather than
    # calling setup_complete straight from `bench execute` with JSON.
    setup_complete(frappe._dict(_COMPANY_ARGS))
    _ensure_fiscal_years()
    frappe.db.commit()
    print("ERPNext setup complete: company '{0}' and default masters created".format(
        _COMPANY_ARGS["company_name"]))
