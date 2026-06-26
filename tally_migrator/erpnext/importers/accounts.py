"""Chart of Accounts and Cost Centre importers."""

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
from .base import BaseImporter, ImportResult
from .banks import _ensure_bank, _insert_bank_account

# ── Chart of Accounts importer ───────────────────────────────────────────────

class AccountImporter:
    """Creates the Chart of Accounts (groups + ledger accounts).

    Two modes:
    - ``reuse``  (default): Tally's reserved groups are NOT recreated; their
      ERPNext standard-COA equivalents are used as parents. Only custom groups
      and ledger accounts are created.
    - ``mirror`` : every Tally group is recreated verbatim.

    Parties (ledgers under Sundry Debtors/Creditors) are excluded upstream by the
    extractor - they are Customers/Suppliers, not ledger Accounts.

    Standalone (not a BaseImporter) because parent resolution + topological group
    ordering don't fit the simple key_field upsert template.
    """

    doctype = "Account"

    def __init__(self, company: str, abbr: str, mode: str = "reuse"):
        self.company = company
        self.abbr = abbr
        self.mode = mode if mode in ("reuse", "mirror") else "reuse"
        self._group_cache: dict[str, str] = {}

    def run(self, accounts: list) -> ImportResult:
        result = ImportResult(self.doctype)
        for node in self._ordered(self._select(accounts)):
            parent = self._resolve_parent(node)
            if not parent:
                result.add_error(node.name, "could not resolve a parent account")
                continue
            self._upsert(result, node, parent)
        return result

    # ── Selection + ordering ─────────────────────────────────────────────────
    def _select(self, accounts: list) -> list:
        if self.mode == "mirror":
            return list(accounts)
        # reuse: reserved groups already exist in ERPNext - don't recreate them.
        return [a for a in accounts if not (a.is_group and a.is_reserved)]

    def _ordered(self, nodes: list) -> list:
        groups = [n for n in nodes if n.is_group]
        ledgers = [n for n in nodes if not n.is_group]
        return self._topo_groups(groups) + ledgers

    @staticmethod
    def _topo_groups(groups: list) -> list:
        index = {g.name: g for g in groups}
        ordered, visited, visiting = [], set(), set()   # visiting = cycle guard

        def visit(name: str) -> None:
            if name in visited or name not in index:
                return
            visiting.add(name)
            parent = index[name].parent
            if parent in index and parent not in visiting:
                visit(parent)
            visiting.discard(name)
            visited.add(name)
            ordered.append(index[name])

        for g in groups:
            visit(g.name)
        return ordered

    # ── Parent resolution ────────────────────────────────────────────────────
    def _erp_name(self, base: str) -> str:
        return company_scoped(base, self.abbr)

    def _resolve_parent(self, node) -> str | None:
        parent = node.parent
        if self.mode == "mirror":
            return self._erp_name(parent) if parent else self._root_group(node.root_type)
        if not parent:
            return self._root_group(node.root_type)
        cls = classify_group(parent)
        if cls:  # parent is a reserved group → use its ERPNext default group
            return self._default_group(cls["erpnext_group"], node.root_type)
        return self._erp_name(parent)  # custom parent was (or will be) recreated

    def _default_group(self, base: str, root_type: str) -> str | None:
        if base in self._group_cache:
            return self._group_cache[base]
        candidate = self._erp_name(base)
        resolved = candidate if frappe.db.exists("Account", candidate) else self._root_group(root_type)
        if resolved:
            self._group_cache[base] = resolved
        return resolved

    def _root_group(self, root_type: str) -> str | None:
        base = ERPNEXT_ROOT_GROUPS.get(root_type)
        if base:
            candidate = self._erp_name(base)
            if frappe.db.exists("Account", candidate):
                return candidate
        rows = frappe.get_all(
            "Account", fields=["name", "parent_account"],
            filters={"root_type": root_type, "is_group": 1, "company": self.company},
        )
        for r in rows:
            if not r.get("parent_account"):
                return r["name"]
        return rows[0]["name"] if rows else None

    def _upsert(self, result: ImportResult, node, parent: str) -> None:
        try:
            existing = frappe.db.get_value(
                "Account", self._erp_name(node.name), ["name", "is_group"], as_dict=True)
            if existing:
                # The name is already taken. MariaDB's default collation is
                # case-insensitive, so a Tally custom group ("OFFICE EQUIPMENT")
                # collides with a standard-CoA *ledger* of the same name
                # ("Office Equipment", a Fixed Asset ledger). Skipping would leave a
                # ledger where a group is needed, and every child would then fail with
                # "Parent account ... can not be a ledger". When we need a group but the
                # existing account is a ledger, promote it so the children can nest.
                if node.is_group and not existing.is_group:
                    self._promote_to_group(result, existing.name, node)
                else:
                    result.skipped += 1
                return
            doc = {
                "doctype": "Account",
                "account_name": node.name,
                "company": self.company,
                "parent_account": parent,
                "is_group": 1 if node.is_group else 0,
                "root_type": node.root_type,
            }
            if node.account_type:
                doc["account_type"] = node.account_type
            d = frappe.get_doc(doc)
            d.insert(ignore_permissions=True)
            frappe.db.commit()
            result.add_created(d.name)
        except Exception as exc:
            result.add_error(node.name, exc)
            frappe.db.rollback()
            return
        # A Tally bank ledger carries the company's own account no + IFSC → create a
        # company Bank Account linked to this GL account. Separate, non-fatal step so
        # a Bank-Account quirk can't roll back the account that was just created.
        if not node.is_group and node.account_type == "Bank" and node.bank_account_no:
            self._save_company_bank_account(node, d.name, result)

    def _promote_to_group(self, result: ImportResult, account_name: str, node) -> None:
        """Convert an existing ledger Account to a group so Tally's children can nest.

        Reached only when a Tally group's name collides (case-insensitively) with an
        existing ledger - typically an empty standard-CoA placeholder ledger. The
        ledger is converted in place; ERPNext rebuilds the tree. A converted account
        is reported as created (it is now the group the migration needs) plus a warning
        so the reuse is visible in the log.
        """
        try:
            acc = frappe.get_doc("Account", account_name)
            if acc.check_gle_exists():
                # Only possible on a re-run after transactions posted; ERPNext (rightly)
                # forbids converting a transacting account. Surface it instead of crashing.
                result.add_error(
                    node.name,
                    f"could not create group '{node.name}': an account named "
                    f"'{account_name}' already exists and has transactions, so it "
                    "cannot be converted to a group")
                return
            # These placeholder ledgers carry an account_type (e.g. Fixed Asset, Tax);
            # the flag lets a typed account become a group, which ERPNext otherwise blocks.
            acc.flags.exclude_account_type_check = True
            acc.flags.ignore_permissions = True
            acc.convert_ledger_to_group()
            frappe.db.commit()
            result.add_created(acc.name)
            result.add_warning(
                node.name,
                f"reused existing account '{account_name}' and converted it to a group "
                "so the Tally sub-accounts under it could be created")
        except Exception as exc:
            frappe.db.rollback()
            result.add_error(node.name, exc)

    def _save_company_bank_account(self, node, account_name: str,
                                   result: ImportResult) -> None:
        bank = _ensure_bank(node.bank_name, result, is_company=True)
        if not bank:
            result.add_warning(
                node.name, "bank account not created: no bank name on the ledger")
            return
        _insert_bank_account(
            account_name=node.bank_holder or node.name,
            bank=bank,
            account_no=node.bank_account_no,
            ifsc=node.bank_ifsc,
            gl_account=account_name,        # link to the GL account just created
            is_company=True,
            result=result,
            warn_name=node.name,
            count_created=True,
        )


# ── Cost Centre importer ─────────────────────────────────────────────────────

class CostCentreImporter:
    """Creates Cost Centers (flat or nested) under the company's root centre."""

    doctype = "Cost Center"

    def __init__(self, company: str, abbr: str):
        self.company = company
        self.abbr = abbr

    def run(self, centres: list) -> ImportResult:
        result = ImportResult(self.doctype)
        names = {c.name for c in centres}
        parents = {c.parent for c in centres if c.parent}
        root = self._root_centre()
        if not root:
            for c in centres:
                result.add_error(c.name, "no root cost center found in ERPNext")
            return result
        for node in self._ordered(centres):
            parent = self._erp_name(node.parent) if node.parent in names else root
            self._upsert(result, node, node.name in parents, parent)
        return result

    def _erp_name(self, base: str) -> str:
        return company_scoped(base, self.abbr)

    @staticmethod
    def _ordered(centres: list) -> list:
        index = {c.name: c for c in centres}
        ordered, visited, visiting = [], set(), set()   # visiting = cycle guard

        def visit(name: str) -> None:
            if name in visited or name not in index:
                return
            visiting.add(name)
            parent = index[name].parent
            if parent in index and parent not in visiting:
                visit(parent)
            visiting.discard(name)
            visited.add(name)
            ordered.append(index[name])

        for c in centres:
            visit(c.name)
        return ordered

    def _root_centre(self) -> str | None:
        rows = frappe.get_all(
            "Cost Center", fields=["name", "parent_cost_center"],
            filters={"company": self.company, "is_group": 1},
        )
        for r in rows:
            if not r.get("parent_cost_center"):
                return r["name"]
        return rows[0]["name"] if rows else None

    def _upsert(self, result: ImportResult, node, is_group: bool, parent: str) -> None:
        try:
            if frappe.db.exists("Cost Center", self._erp_name(node.name)):
                result.skipped += 1
                return
            d = frappe.get_doc({
                "doctype": "Cost Center",
                "cost_center_name": node.name,
                "parent_cost_center": parent,
                "company": self.company,
                "is_group": 1 if is_group else 0,
            })
            d.insert(ignore_permissions=True)
            frappe.db.commit()
            result.add_created(d.name)
        except Exception as exc:
            result.add_error(node.name, exc)
            frappe.db.rollback()
