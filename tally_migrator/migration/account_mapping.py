"""Read-only projection: how each Tally ledger account becomes an ERPNext account.

Powers the Step-4 "Review accounts" screen. It is a pure read-over of what the
COA extraction already computes (root_type / account_type / parent / opening
balance) plus two derived signals the user actually cares about before they
commit:

  * **inferred** - the ledger's group is not a named standard Tally group, so its
    type was either *derived* from the group's own nature flags (ISREVENUE /
    ISDEEMEDPOSITIVE) or, failing that, left *unknown* (shown as "--"). These are
    the rows worth eyeballing; everything else mapped by Tally's documented groups
    and is high-confidence.
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
from tally_migrator.tally.resolver import LedgerResolver

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
        # source: "reserved" = mapped by a named standard group (high confidence);
        # "derived" = inferred from the group's own Tally nature flags; "unknown" =
        # neither, so the type is unresolved and shown as "--" for the user to set.
        source = resolver.group_nature(a.parent).get("source")
        is_inferred = source != "reserved"
        row = {
            "name": a.name,
            "root_type": a.root_type,
            "account_type": a.account_type,
            "parent": a.parent,
            "amount": round(a.opening_balance, 2),
            "dr_cr": a.opening_dr_cr,
            "inferred": is_inferred,
            "uncertain": source == "unknown",
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
        "opening": _opening_plug(coa, masters),
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

    Foreign-currency parties are posted in their own currency (one invoice per
    foreign-amount bill, else a single invoice) and counted here too, mirroring
    ``PartyOpeningImporter._emit_forex``; ``foreign`` reports how many there were.
    """
    by_party: dict[str, list] = {}
    for b in bills or []:
        by_party.setdefault(b.party, []).append(b)

    invoices = advances = on_account = lump = 0
    party_count = foreign = 0
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
            party_count += 1
            ledger_signed = _signed(ledger_amt, ledger_drcr)

            # Forex party: posted in its own currency by PartyOpeningImporter._emit_forex
            # (detected by CurrencyISO, resolved from the opening-string symbol - the same
            # signal the importer uses, NOT CurrencyName, which a real export omits on a
            # forex party). Mirror its document count: one invoice per bill that carries a
            # foreign amount (+ a remainder), else a single consolidated invoice.
            if (record.get("CurrencyISO") or "").strip():
                foreign += 1
                p_invoices = p_lump = 0
                if _classify_party(party_type, ledger_signed, False) == "invoice":
                    fbills = [
                        b for b in party_bills
                        if b.foreign_amount and _classify_party(
                            party_type, _signed(b.amount, b.dr_cr), b.is_advance) == "invoice"]
                    if fbills:
                        p_invoices = len(fbills)
                        posted = sum(_signed(b.amount, b.dr_cr) for b in fbills)
                        residual = round(ledger_signed - posted, 2)
                        if (abs(residual) >= _PARTY_PLUG_THRESHOLD
                                and _classify_party(party_type, residual, False) == "invoice"):
                            p_invoices += 1     # remainder 'Opening' invoice
                    else:
                        p_lump = 1              # consolidated single invoice
                invoices += p_invoices
                lump += p_lump
                parties_list.append({
                    "name": name, "party_type": party_type,
                    "amount": round(ledger_amt, 2), "dr_cr": ledger_drcr,
                    "invoices": p_invoices, "advances": 0, "on_account": 0,
                    "documents": p_invoices + p_lump, "foreign": True,
                })
                continue

            bills_signed = 0.0
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
        # Foreign-currency parties (posted in their own currency, see _emit_forex).
        # Their documents are already included in the counts above; this is just how
        # many of the parties were foreign, so the screen can call it out.
        "foreign": foreign,
        "mismatches": mismatches,
        "parties_list": parties_list,
    }


def _opening_plug(coa, masters) -> dict:
    """Net residual across the whole opening trial balance → Temporary Opening.

    Delegates to ``reconciliation.source_totals`` so the plug shown on the Review
    step is computed by the SAME function that builds the Log's trial balance - one
    source of truth, so the two screens can never disagree. That residual is the
    contra of every opening posting the migration makes: ledger-account openings
    (the opening JE), party openings (opening invoices) AND opening stock (the stock
    reconciliation) - all of which post against 'Temporary Opening' (see
    ``OpeningBalanceImporter`` and ``StockOpeningImporter``). An earlier version
    here omitted opening stock and so understated the plug on any file with opening
    inventory; ``source_totals`` includes it, which is what actually posts.
    """
    from tally_migrator.migration.reconciliation import source_totals

    totals = source_totals(coa, masters)
    temp = totals["temporary_opening"]
    plug = round(temp["amount"], 2)
    # Total opening value = the magnitude of every row in the trial balance, so the
    # UI can show the plug as a share of the whole (a small gap vs a structural one).
    gross = (
        sum(abs(c["side"]["amount"]) for c in totals["classes"])
        + abs(totals["receivables"]["amount"])
        + abs(totals["payables"]["amount"])
        + abs(totals["stock"]["amount"])
    )
    return {
        "temporary_opening_plug": plug,
        "plug_dr_cr": temp["dr_cr"],          # already "" when the plug is clean
        "clean": plug < _PLUG_EPSILON,
        "gross_opening": round(gross, 2),
    }
