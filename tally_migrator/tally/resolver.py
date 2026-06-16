"""
Ledger resolver - the single source of truth for "what does this Tally ledger
become in ERPNext".

Built once from the Tally group tree + ledgers, then consulted by COA extraction
to classify non-party ledgers into Accounts with a nature (root_type +
account_type), and to walk a group up to its nearest reserved ancestor.

Pure - no Frappe. Classification only; turning a target into a concrete ERPNext
name is the importer's job (it needs the company abbreviation, known only at
import time).

Ported from the sibling Tally Bridge app (tally_bridge/tally/resolver.py).
"""
from __future__ import annotations

from dataclasses import dataclass

from .mappings import (
    ASSET,
    CREDITOR_ROOTS,
    DEBTOR_ROOTS,
    EXPENSE,
    INCOME,
    LIABILITY,
    classify_group,
)

CUSTOMER = "customer"
SUPPLIER = "supplier"
ACCOUNT = "account"

# Last resort when a ledger's group has neither a reserved ancestor nor its own
# nature flags. We still create the account (ERPNext needs *some* root_type), but
# default it to Asset and mark the nature "unknown" so the preview shows it as
# unresolved ("--") rather than asserting a type we don't actually know.
FALLBACK_NATURE = {"root": ASSET, "account_type": "", "erpnext_group": "Current Assets"}

# Tally stamps every group - reserved or custom - with two booleans that together
# *are* its nature, and which Tally itself uses to place the group on the balance
# sheet vs the P&L and to set its debit/credit side:
#   ISREVENUE        - Yes = P&L (Income/Expense), No = Balance Sheet (Asset/Liability)
#   ISDEEMEDPOSITIVE - Yes = debit-nature (Asset/Expense), No = credit-nature
# This 2x2 recovers the ERPNext root_type with no name list - the derive-don't-
# enumerate path - for any custom group with no reserved ancestor. Equity is not
# distinguishable from Liability by these flags (Capital is credit-nature balance
# sheet too), but equity lives under the reserved "Capital Account" group, which the
# reserved map already classifies, so a *custom* group resolving here is a liability.
# Income/Expense leaves carry the matching ERPNext account_type; Asset/Liability
# leaves stay ordinary ("").
_DERIVED_NATURE = {
    #  (is_revenue, is_deemed_positive)
    (False, True):  {"root": ASSET,     "account_type": "",                "erpnext_group": "Current Assets"},
    (False, False): {"root": LIABILITY, "account_type": "",                "erpnext_group": "Current Liabilities"},
    (True,  True):  {"root": EXPENSE,   "account_type": "Expense Account", "erpnext_group": "Indirect Expenses"},
    (True,  False): {"root": INCOME,    "account_type": "Income Account",  "erpnext_group": "Indirect Income"},
}


@dataclass
class LedgerTarget:
    tally_name: str
    kind: str                # CUSTOMER | SUPPLIER | ACCOUNT
    root_type: str = ""      # ACCOUNT only
    account_type: str = ""   # ACCOUNT only ("" = ordinary; "Bank"/"Tax"/… = special)


class LedgerResolver:
    def __init__(self, groups: list[dict], ledgers: list[dict] | None = None):
        self._parent_of = {g["_name"]: g.get("Parent", "").strip() for g in groups}
        # Each group's own Tally nature flags, normalised to "yes"/"no"/"" - used to
        # derive a root_type when no reserved ancestor exists (see group_nature).
        self._flags_of = {
            g["_name"]: (
                (g.get("IsRevenue") or "").strip().lower(),
                (g.get("IsDeemedPositive") or "").strip().lower(),
            )
            for g in groups
        }
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
        """Classify a group, most-confident signal first. The returned dict carries a
        ``source``: ``reserved`` (a named standard Tally group - high confidence),
        ``derived`` (inferred from the group's own ISREVENUE/ISDEEMEDPOSITIVE flags),
        or ``unknown`` (neither available - defaults to Asset, flagged for review)."""
        # 1. nearest reserved ancestor - a named standard group, high confidence.
        seen: set[str] = set()
        cur = group_name
        while cur and cur not in seen:
            seen.add(cur)
            cls = classify_group(cur)
            if cls:
                return {**cls, "source": "reserved"}
            cur = self._parent_of.get(cur, "")
        # 2. derive from the nearest ancestor carrying Tally's own nature flags.
        seen, cur = set(), group_name
        while cur and cur not in seen:
            seen.add(cur)
            rev, pos = self._flags_of.get(cur, ("", ""))
            if rev in ("yes", "no") and pos in ("yes", "no"):
                nature = _DERIVED_NATURE[(rev == "yes", pos == "yes")]
                return {**nature, "source": "derived"}
            cur = self._parent_of.get(cur, "")
        # 3. genuinely unresolved.
        return {**FALLBACK_NATURE, "source": "unknown"}

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
