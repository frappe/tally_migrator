"""Bank / Bank Account helpers, shared by party and company-account importers."""

import contextlib
from collections import Counter
from dataclasses import dataclass, field

import frappe

from tally_migrator.tally.mappings import (
    UOM_MAP,
    TALLY_STATE_MAP,
    DEFAULT_CUSTOMER_GROUP,
    DEFAULT_SUPPLIER_GROUP,
    DEFAULT_ITEM_GROUP,
    DEFAULT_TERRITORY,
    DEFAULT_WAREHOUSE,
    DEFAULT_UOM,
    ERPNEXT_ROOT_GROUPS,
    classify_group,
    gst_category_from_type,
)
from tally_migrator.naming import safe_item_code, company_scoped
from tally_migrator.tally.extractors import TallyExtractor
from tally_migrator.validation.engine import (
    infer_gst_category, validate_gstin, GSTIN_STATE_CODES,
)
from .base import ImportResult

# ── Bank Account helpers (shared by party + company bank accounts) ─────────────

def _ensure_bank(bank_name: str, result: "ImportResult | None" = None) -> str:
    """Return an existing/created Bank master name, or "" when no name is given.

    ERPNext's Bank Account requires a linked Bank; Tally only gives us its name,
    so we create the Bank master on demand. Best-effort: a failure returns "" and
    the caller skips bank-account creation with a warning rather than aborting.

    A Bank we actually create is recorded on ``result`` so revert removes it too
    (the linked Bank Accounts are deleted first - company ones in the same manifest
    bucket, party ones when the party goes - and the Bank delete is unforced, so one
    still referenced elsewhere is safely kept). An already-existing Bank is never
    recorded, so a pre-existing Bank is left untouched."""
    name = (bank_name or "").strip()
    if not name:
        return ""
    if frappe.db.exists("Bank", name):
        return name
    try:
        frappe.get_doc({"doctype": "Bank", "bank_name": name}).insert(ignore_permissions=True)
        frappe.db.commit()
        if result is not None:
            result.add_created(name, "Bank")
        return name
    except Exception:
        frappe.db.rollback()
        return ""


def _insert_bank_account(*, account_name: str, bank: str, account_no: str,
                         ifsc: str, result: "ImportResult", warn_name: str,
                         party_type: str = "", party: str = "",
                         gl_account: str = "", is_company: bool = False,
                         count_created: bool = False) -> str:
    """Insert one Bank Account doc, shared by the party and company bank paths.

    Both paths build the same doc (account name + bank + account no + IFSC) and
    differ only in how it's linked: a party account points at a Customer/Supplier
    (``party_type``/``party``), a company account at the GL account and sets
    ``is_company_account`` (``gl_account``/``is_company``). Non-fatal - a failure
    is logged, recorded as a warning, rolled back, and "" returned so a Bank
    Account quirk never aborts the party/account that was just created.
    """
    try:
        ba = frappe.new_doc("Bank Account")
        ba.account_name = account_name
        ba.bank = bank
        ba.bank_account_no = account_no
        if ifsc:
            ba.branch_code = ifsc
        if is_company:
            ba.account = gl_account
            ba.is_company_account = 1
        else:
            ba.party_type = party_type
            ba.party = party
        ba.insert(ignore_permissions=True)
        frappe.db.commit()
        if count_created:
            result.add_created(ba.name, "Bank Account")
        return ba.name
    except Exception as exc:
        frappe.log_error("Tally Migrator", f"Bank account save failed for {warn_name}: {exc}")
        result.add_warning(warn_name, f"bank account not created: {exc}")
        frappe.db.rollback()
        return ""
