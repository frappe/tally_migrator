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
from .base import ImportResult, atomic

# ── Bank Account helpers (shared by party + company bank accounts) ─────────────

def _unique_bank_account_name(account_name: str, bank: str, account_no: str = "") -> str:
    """An account_name that yields a unique Bank Account, given ERPNext names the doc
    ``account_name + " - " + bank`` (Bank Account.autoname).

    Tally often repeats one account-holder name across several bank ledgers of the
    same company (e.g. every HDFC ledger carries the holder 'ICONCEPT'). They then
    collapse to the same Bank Account name and all but the first fail on a duplicate
    key, silently dropping that ledger's bank details. So when the natural name is
    taken, disambiguate - first with the account number (unique per real account),
    then with a numeric suffix - so each bank ledger keeps its own Bank Account.
    Generic: a holder that is already unique is returned unchanged."""
    base = (account_name or "").strip()
    if not base or not frappe.db.exists("Bank Account", f"{base} - {bank}"):
        return base
    acc_no = (account_no or "").strip()
    if acc_no:
        candidate = f"{base} ({acc_no})"
        if not frappe.db.exists("Bank Account", f"{candidate} - {bank}"):
            return candidate
    i = 2
    while frappe.db.exists("Bank Account", f"{base} {i} - {bank}"):
        i += 1
    return f"{base} {i}"

def _ensure_bank(bank_name: str, result: "ImportResult | None" = None, *,
                 is_company: bool = False) -> str:
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
        # Savepoint-isolate the insert so a failure rolls back only this Bank, never a
        # caller's uncommitted work.
        with atomic():
            frappe.get_doc({"doctype": "Bank", "bank_name": name}).insert(ignore_permissions=True)
        # Commit only on the non-batched company path: AccountImporter is standalone and
        # a *later* account's full rollback (accounts.py) would otherwise wipe this
        # still-uncommitted Bank. The party path is batched and savepoint-isolated with
        # no full rollback, so the Bank is safe in the batch until run()'s batch commit -
        # committing here would flush the whole in-flight batch and defeat the batching.
        if is_company:
            frappe.db.commit()
        if result is not None:
            result.add_created(name, "Bank")
        return name
    except Exception:
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
        # Savepoint-isolated (see _ensure_bank): a failure rolls back only this Bank
        # Account, never a caller's uncommitted batch.
        with atomic():
            ba = frappe.new_doc("Bank Account")
            # Disambiguate a holder name already used by another bank ledger so the
            # autonamed doc ("account_name - bank") does not collide and drop this
            # ledger's bank details.
            ba.account_name = _unique_bank_account_name(account_name, bank, account_no)
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
        # Commit only on the non-batched company path, for the same reason as
        # _ensure_bank: the party path leaves this in the batch for run()'s commit so
        # batching is preserved, while the company path must persist it before a later
        # account's full rollback can wipe it.
        if is_company:
            frappe.db.commit()
        if count_created:
            result.add_created(ba.name, "Bank Account")
        return ba.name
    except Exception as exc:
        frappe.log_error("Tally Migrator", f"Bank account save failed for {warn_name}: {exc}")
        result.add_warning(warn_name, f"bank account not created: {exc}")
        return ""
