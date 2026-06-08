"""Company readiness pre-flight.

Before a single record is imported, check that the *target* ERPNext company is
actually set up to receive masters. Without this, missing prerequisites surface
only as per-record failures mid-run (e.g. "Customer Group 'Commercial' not found"
repeated for every customer) — confusing and late. This turns them into one clear
"your company isn't ready" panel on the pre-flight screen.

Severity model
--------------
- **blocker**  — an entire entity would fail to import (e.g. the default Customer
  Group is missing → every customer errors). The UI blocks the run.
- **warning**  — a part of the migration is degraded but masters still import
  (e.g. no Receivable account → party opening balances are skipped).

Needs Frappe (it queries the live company setup), so it is not in the pure-test
set; its shape is stable and the importers it guards are integration-tested.
"""
import frappe

from tally_migrator.tally.mappings import (
    DEFAULT_CUSTOMER_GROUP,
    DEFAULT_SUPPLIER_GROUP,
    DEFAULT_ITEM_GROUP,
    DEFAULT_TERRITORY,
    DEFAULT_WAREHOUSE,
)
from tally_migrator.naming import company_scoped


def _issue(code: str, message: str, fix: str) -> dict:
    return {"code": code, "message": message, "fix": fix}


def check_readiness(company: str) -> dict:
    """Inspect ``company`` and report blockers + warnings.

    Returns ``{"ready": bool, "company": str, "blockers": [...], "warnings": [...]}``
    where ``ready`` is True only when there are no blockers.
    """
    if not company:
        return {
            "ready": False, "company": "",
            "blockers": [_issue(
                "NO_COMPANY", "No ERPNext company selected.",
                "Pick the company that will receive these records.")],
            "warnings": [],
        }
    if not frappe.db.exists("Company", company):
        return {
            "ready": False, "company": company,
            "blockers": [_issue(
                "NO_COMPANY", f"Company '{company}' does not exist in ERPNext.",
                "Create the company first, then return here.")],
            "warnings": [],
        }

    abbr = frappe.get_value("Company", company, "abbr") or ""
    blockers: list[dict] = []
    warnings: list[dict] = []

    # ── Blockers: a whole entity would fail without these ────────────────────
    # Customer Group must exist AND be a leaf (ERPNext rejects a group node on a
    # Customer).
    cg = frappe.db.get_value("Customer Group", DEFAULT_CUSTOMER_GROUP, ["name", "is_group"], as_dict=True)
    if not cg:
        blockers.append(_issue(
            "CUSTOMER_GROUP_MISSING",
            f"Default Customer Group '{DEFAULT_CUSTOMER_GROUP}' is missing — every customer would fail.",
            f"Create a leaf Customer Group named '{DEFAULT_CUSTOMER_GROUP}'."))
    elif cg.is_group:
        blockers.append(_issue(
            "CUSTOMER_GROUP_NOT_LEAF",
            f"Customer Group '{DEFAULT_CUSTOMER_GROUP}' is a group node — ERPNext won't accept it on a customer.",
            "Use a non-group (leaf) Customer Group."))

    if not frappe.db.exists("Supplier Group", DEFAULT_SUPPLIER_GROUP):
        blockers.append(_issue(
            "SUPPLIER_GROUP_MISSING",
            f"Default Supplier Group '{DEFAULT_SUPPLIER_GROUP}' is missing — every supplier would fail.",
            f"Create a Supplier Group named '{DEFAULT_SUPPLIER_GROUP}'."))

    if not frappe.db.exists("Item Group", DEFAULT_ITEM_GROUP):
        blockers.append(_issue(
            "ITEM_GROUP_MISSING",
            f"Default Item Group '{DEFAULT_ITEM_GROUP}' is missing — items would fail.",
            f"Create an Item Group named '{DEFAULT_ITEM_GROUP}'."))

    if not frappe.db.exists("Territory", DEFAULT_TERRITORY):
        blockers.append(_issue(
            "TERRITORY_MISSING",
            f"Default Territory '{DEFAULT_TERRITORY}' is missing — every customer would fail.",
            f"Create a Territory named '{DEFAULT_TERRITORY}'."))

    # ── Warnings: partial degradation, masters still import ──────────────────
    if not frappe.get_cached_value("Company", company, "default_receivable_account"):
        warnings.append(_issue(
            "NO_RECEIVABLE_ACCOUNT",
            "Company has no default Receivable account — customer opening balances will be skipped.",
            "Set Default Receivable Account on the Company, or migrate customer openings later."))

    if not frappe.get_cached_value("Company", company, "default_payable_account"):
        warnings.append(_issue(
            "NO_PAYABLE_ACCOUNT",
            "Company has no default Payable account — supplier opening balances will be skipped.",
            "Set Default Payable Account on the Company, or migrate supplier openings later."))

    if not _has_fiscal_year(company):
        warnings.append(_issue(
            "NO_FISCAL_YEAR",
            "No Fiscal Year covers today's date — opening Journal Entry / Stock Reconciliation can't post.",
            "Create the current Fiscal Year in ERPNext before migrating opening balances."))

    root_wh = company_scoped(DEFAULT_WAREHOUSE, abbr)
    if not frappe.db.exists("Warehouse", root_wh):
        warnings.append(_issue(
            "NO_ROOT_WAREHOUSE",
            f"Root warehouse '{root_wh}' not found — opening stock may have nowhere to land.",
            "Ensure the company's warehouse tree exists (usually auto-created with the company)."))

    return {
        "ready": not blockers,
        "company": company,
        "blockers": blockers,
        "warnings": warnings,
    }


def _has_fiscal_year(company: str) -> bool:
    try:
        from erpnext.accounts.utils import get_fiscal_year
        get_fiscal_year(frappe.utils.nowdate(), company=company)
        return True
    except Exception:
        return False
