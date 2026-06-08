"""
Ledger resolver — the single source of truth for "what does this Tally ledger
become in ERPNext".

Built once from the Tally group tree + ledgers, then consulted by COA extraction
to classify non-party ledgers into Accounts with a nature (root_type +
account_type), and to walk a group up to its nearest reserved ancestor.

Pure — no Frappe. Classification only; turning a target into a concrete ERPNext
name is the importer's job (it needs the company abbreviation, known only at
import time).

Ported from the sibling Tally Bridge app (tally_bridge/tally/resolver.py).
"""
from __future__ import annotations

from dataclasses import dataclass

from .mappings import ASSET, CREDITOR_ROOTS, DEBTOR_ROOTS, classify_group

CUSTOMER = "customer"
SUPPLIER = "supplier"
ACCOUNT = "account"

# Used when a ledger's group chain has no reserved ancestor (shouldn't happen in
# real Tally, where every group descends from a primary group).
FALLBACK_NATURE = {"root": ASSET, "account_type": "", "erpnext_group": "Current Assets"}


@dataclass
class LedgerTarget:
    tally_name: str
    kind: str                # CUSTOMER | SUPPLIER | ACCOUNT
    root_type: str = ""      # ACCOUNT only
    account_type: str = ""   # ACCOUNT only ("" = ordinary; "Bank"/"Tax"/… = special)


class LedgerResolver:
    def __init__(self, groups: list[dict], ledgers: list[dict] | None = None):
        self._parent_of = {g["_name"]: g.get("Parent", "").strip() for g in groups}
        self._debtor_groups = self._descendants(DEBTOR_ROOTS)
        self._creditor_groups = self._descendants(CREDITOR_ROOTS)
        self._by_name: dict[str, LedgerTarget] = {}
        for ledger in ledgers or []:
            name = ledger["_name"]
            self._by_name[name] = self._classify(name, ledger.get("Parent", "").strip())

    # ── Public ───────────────────────────────────────────────────────────────

    def resolve(self, ledger_name: str) -> LedgerTarget | None:
        """Return the target for a known ledger, else None."""
        return self._by_name.get(ledger_name)

    def kind_of(self, ledger_name: str) -> str | None:
        target = self._by_name.get(ledger_name)
        return target.kind if target else None

    def group_nature(self, group_name: str) -> dict:
        """Classify a group by walking up to its nearest reserved ancestor."""
        seen: set[str] = set()
        cur = group_name
        while cur and cur not in seen:
            seen.add(cur)
            cls = classify_group(cur)
            if cls:
                return cls
            cur = self._parent_of.get(cur, "")
        return FALLBACK_NATURE

    # ── Internals ────────────────────────────────────────────────────────────

    def _classify(self, name: str, parent: str) -> LedgerTarget:
        if parent in self._debtor_groups:
            return LedgerTarget(name, CUSTOMER)
        if parent in self._creditor_groups:
            return LedgerTarget(name, SUPPLIER)
        nature = self.group_nature(parent)
        return LedgerTarget(name, ACCOUNT, nature["root"], nature["account_type"])

    def _descendants(self, roots: set[str]) -> set[str]:
        """All groups nested under the given roots (arbitrary depth)."""
        result, changed = set(roots), True
        while changed:
            changed = False
            for name, parent in self._parent_of.items():
                if name not in result and parent in result:
                    result.add(name)
                    changed = True
        return result
