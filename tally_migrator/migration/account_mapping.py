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

# Mirror of PartyOpeningImporter._PLUG_THRESHOLD - a bill/residual below this is
# rounding noise and is not emitted as its own opening document.
_PARTY_PLUG_THRESHOLD = 1.0


def account_mapping(source) -> dict:
    """Build the accounts-mapping preview for an uploaded Tally masters file."""
    extractor = TallyExtractor(source)
    coa = extractor.extract_coa()
    masters = extractor.extract_all()
    bills = extractor.extract_bill_allocations()

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
        "party_openings": _party_openings(masters, bills),
    }


def _signed(amount: float, dr_cr: str) -> float:
    """Dr is positive, Cr negative - the convention the opening JE balances on."""
    return amount if dr_cr == "Dr" else -amount


def _classify_party(party_type: str, signed: float, is_advance: bool) -> str:
    """Mirror of PartyOpeningImporter._classify: an outstanding invoice (party's
    natural side, not flagged advance) vs an advance/credit. Customer natural side
    = Dr (signed > 0); Supplier = Cr (signed < 0); Tally's ISADVANCE forces advance."""
    if is_advance:
        return "advance"
    on_natural_side = (signed > 0) if party_type == "Customer" else (signed < 0)
    return "invoice" if on_natural_side else "advance"


def _party_openings(masters, bills) -> dict:
    """Preview of how party (customer/supplier) opening balances will post,
    invoice-wise. Read-only twin of ``PartyOpeningImporter``: it groups bills per
    party and applies the *same* classification and residual logic, but counts
    documents instead of creating them. The four buckets are exactly what the
    importer emits:

      * **invoices**   - outstanding bills on the party's natural side -> one
        opening Sales/Purchase Invoice each.
      * **advances**   - ISADVANCE bills (or bills on the opposite side) -> one
        opening Payment Entry each.
      * **on_account** - parties whose bills did not add up to the ledger opening;
        the gap posts as one 'On Account' opening (a mismatch worth reviewing).
      * **lump**       - parties with an opening but no bill detail -> a single
        opening document for the whole balance.
    """
    by_party: dict[str, list] = {}
    for b in bills or []:
        by_party.setdefault(b.party, []).append(b)

    # Tally base currency (modal ledger CurrencyName). A forex party is skipped by
    # PartyOpeningImporter, so the preview skips it too and the doc counts match what
    # actually posts. Equality only, never an ISO-code mapping.
    from collections import Counter
    _ccy = Counter(
        (r.get("CurrencyName") or "").strip()
        for r in (*masters.customers, *masters.suppliers)
        if (r.get("CurrencyName") or "").strip()
    )
    base_ccy = _ccy.most_common(1)[0][0] if _ccy else ""

    invoices = advances = on_account = lump = 0
    party_count = foreign_skipped = 0
    mismatches: list[dict] = []
    parties_list: list[dict] = []
    for party_type, records in (("Customer", masters.customers),
                                ("Supplier", masters.suppliers)):
        for record in records:
            name = record.get("_name", "")
            ledger_amt, ledger_drcr = TallyExtractor._parse_opening(
                record.get("OpeningBalance", ""))
            party_bills = by_party.get(name, [])
            if not ledger_amt and not party_bills:
                continue
            # Forex party: skipped at import (currency unknown), so don't count its
            # documents here either. Surfaced as a count so the screen can say so.
            ccy = (record.get("CurrencyName") or "").strip()
            if base_ccy and ccy and ccy != base_ccy:
                foreign_skipped += 1
                continue
            party_count += 1

            ledger_signed = _signed(ledger_amt, ledger_drcr)
            bills_signed = 0.0
            p_invoices = p_advances = 0
            for b in party_bills:
                s = _signed(b.amount, b.dr_cr)
                if abs(s) < _PARTY_PLUG_THRESHOLD:
                    continue
                bills_signed += s
                if _classify_party(party_type, s, b.is_advance) == "advance":
                    p_advances += 1
                else:
                    p_invoices += 1
            invoices += p_invoices
            advances += p_advances

            residual = round(ledger_signed - bills_signed, 2)
            p_on_account = p_lump = 0
            if abs(residual) >= _PARTY_PLUG_THRESHOLD:
                if party_bills:
                    on_account += 1
                    p_on_account = round(abs(residual), 2)
                    mismatches.append({
                        "name": name,
                        "party_type": party_type,
                        "amount": p_on_account,
                        # The ledger opening, shown beside the gap so the two
                        # figures don't look contradictory: 'On Account' is the
                        # unreconciled residual, not the party's total opening.
                        "opening": round(ledger_amt, 2),
                        "opening_dr_cr": ledger_drcr,
                    })
                else:
                    lump += 1
                    p_lump = 1

            # Per-party row for the collapsed "all parties" list (mirrors the COA
            # book). Amount/side is the ledger opening; docs is how many opening
            # documents this party produces.
            parties_list.append({
                "name": name,
                "party_type": party_type,
                "amount": round(ledger_amt, 2),
                "dr_cr": ledger_drcr,
                "invoices": p_invoices,
                "advances": p_advances,
                "on_account": p_on_account,
                "documents": p_invoices + p_advances
                             + (1 if p_on_account else 0) + p_lump,
            })

    parties_list.sort(key=lambda r: (r["party_type"], r["name"].lower()))
    return {
        "parties": party_count,
        "invoices": invoices,
        "advances": advances,
        "on_account": on_account,
        "lump": lump,
        "documents": invoices + advances + on_account + lump,
        "foreign_skipped": foreign_skipped,
        "mismatches": mismatches,
        "parties_list": parties_list,
    }


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
