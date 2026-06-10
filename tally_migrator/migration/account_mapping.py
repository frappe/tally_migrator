"""Read-only projection: how each Tally ledger account becomes an ERPNext account.

Powers the Step-4 "Review accounts" screen. It is a pure read-over of what the
COA extraction already computes (root_type / account_type / parent / opening
balance) plus two derived signals the user actually cares about before they
commit:

  * **inferred** - the ledger's group had no reserved Tally ancestor, so its
    nature was *defaulted* (``FALLBACK_NATURE``) rather than read from Tally's
    own group spec. These are the rows worth eyeballing; everything else mapped
    by Tally's documented groups and is high-confidence.
  * **the Temporary Opening plug** - the residual across the *whole* opening
    trial balance (accounts + customers + suppliers). ERPNext absorbs any
    imbalance into 'Temporary Opening', so a large plug is the single most
    important number on the screen: it means part of the trial balance won't
    land where the books expect it. Computed over all three balance sources to
    match what ``OpeningBalanceImporter`` actually posts - an accounts-only
    verdict would mislead, since parties carry opening balances too.

Writes nothing; safe to call in the pre-flight. Derives, never enumerates: the
inferred flag comes from the resolver's own fallback signal, not a tag list.
"""
from __future__ import annotations

from tally_migrator.tally.extractors import (
    TallyExtractor, GROUP_FIELDS, LEDGER_FIELDS, LEDGER_TAGS,
)
from tally_migrator.tally.resolver import LedgerResolver, FALLBACK_NATURE

# ERPNext's canonical root-type order - how an accountant reads a trial balance.
_ROOT_ORDER = ["Asset", "Liability", "Equity", "Income", "Expense"]

# Opening balances within this much of zero are treated as clean (rounding dust).
_PLUG_EPSILON = 0.01


def account_mapping(source) -> dict:
    """Build the accounts-mapping preview for an uploaded Tally masters file."""
    extractor = TallyExtractor(source)
    coa = extractor.extract_coa()
    masters = extractor.extract_all()

    groups = source.get_collection("Group", GROUP_FIELDS)
    ledgers = source.get_collection("Ledger", LEDGER_FIELDS, LEDGER_TAGS)
    resolver = LedgerResolver(groups, ledgers)

    ledger_accounts = [a for a in coa.accounts if not a.is_group]

    by_root: dict[str, list[dict]] = {r: [] for r in _ROOT_ORDER}
    inferred: list[dict] = []
    for a in ledger_accounts:
        # The resolver returns the module-level FALLBACK_NATURE object *by
        # identity* when no reserved ancestor was found - a clean, derived
        # "we had to guess" signal with no hand-maintained list behind it.
        is_inferred = resolver.group_nature(a.parent) is FALLBACK_NATURE
        row = {
            "name": a.name,
            "root_type": a.root_type,
            "account_type": a.account_type,
            "parent": a.parent,
            "amount": round(a.opening_balance, 2),
            "dr_cr": a.opening_dr_cr,
            "inferred": is_inferred,
        }
        by_root.setdefault(a.root_type, []).append(row)
        if is_inferred:
            inferred.append(row)

    groups_out = []
    for root in _ROOT_ORDER:
        rows = by_root.get(root) or []
        if not rows:
            continue
        rows.sort(key=lambda r: r["name"].lower())
        dr = sum(r["amount"] for r in rows if r["dr_cr"] == "Dr")
        cr = sum(r["amount"] for r in rows if r["dr_cr"] == "Cr")
        groups_out.append({
            "root_type": root,
            "accounts": rows,
            "subtotal_dr": round(dr, 2),
            "subtotal_cr": round(cr, 2),
        })

    return {
        "total_accounts": len(ledger_accounts),
        "inferred_count": len(inferred),
        "inferred": inferred,
        "groups": groups_out,
        "opening": _opening_plug(ledger_accounts, masters),
    }


def _signed(amount: float, dr_cr: str) -> float:
    """Dr is positive, Cr negative - the convention the opening JE balances on."""
    return amount if dr_cr == "Dr" else -amount


def _opening_plug(ledger_accounts, masters) -> dict:
    """Net residual across the whole opening trial balance → Temporary Opening.

    Mirrors ``OpeningBalanceImporter``: accounts post against themselves,
    customers against Receivable, suppliers against Payable. Any net difference
    is plugged to 'Temporary Opening', so the absolute net is exactly what that
    plug will be.
    """
    net = sum(_signed(a.opening_balance, a.opening_dr_cr) for a in ledger_accounts)
    for party in list(masters.customers) + list(masters.suppliers):
        amt, drcr = TallyExtractor._parse_opening(party.get("OpeningBalance", ""))
        net += _signed(amt, drcr)

    plug = round(abs(net), 2)
    return {
        "temporary_opening_plug": plug,
        # The plug line carries the opposite sign of the residual to balance it.
        "plug_dr_cr": "" if plug < _PLUG_EPSILON else ("Cr" if net > 0 else "Dr"),
        "clean": plug < _PLUG_EPSILON,
    }
