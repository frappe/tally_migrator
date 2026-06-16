"""Post-import reconciliation: an opening Trial Balance, Tally vs ERPNext (read-only).

After the migration posts, build the opening trial balance two ways - from what
Tally *said* (the extracted masters + COA) and from what ERPNext now *holds* (read
back from the GL / stock) - and store them side by side on the migration log.
Strictly read-only and best-effort: it never changes data and never aborts the run.

Structure (a real trial balance, not a teaser)
----------------------------------------------
Rows are segmented by *what posted them*, which avoids double-counting:
  * **ledger account classes** (Assets / Liabilities / Equity / Income / Expense) -
    the opening Journal Entry. Class comes from each account's ``root_type``, derived
    by the resolver, never a hand-listed name - so any COA works, not just samples.
  * **Receivables / Payables** - the party opening invoices (the Debtors/Creditors
    control accounts), shown on their own lines rather than folded into Assets/
    Liabilities.
  * **Stock value** - the opening stock reconciliation.
  * **Temporary Opening** - the contra for every opening posting. Tally does not
    force openings to net to zero; the residual is its own "Difference in Opening
    Balances", and a faithful migration leaves exactly that here. A non-zero value
    is therefore expected, not a gap. Every row plus this one nets to zero, so
    Total Dr == Total Cr.

Sign convention: every figure flows through ``TallyExtractor._parse_opening``
(positive XML = Cr, negative = Dr - verified against the Tally UI), so an asset
lands Dr and a liability/equity lands Cr automatically; signs are never guessed
from the account type.

``source_totals`` and ``compare`` are pure (no Frappe) and unit-tested offline;
only ``erpnext_totals`` touches the database (Frappe imported lazily there).
"""
from __future__ import annotations

from tally_migrator.tally.extractors import TallyExtractor

# A difference below this (currency units) is rounding noise, not a real variance.
_TOLERANCE = 1.0

# Trial-balance classes in reading order, with display labels. Liability and Equity
# are merged into one "Liabilities & Equity" row on purpose: Tally and ERPNext
# disagree on where owner's capital sits (Tally treats it as Equity, several ERPNext
# charts root it under Liabilities), so comparing them as separate rows produces a
# false mismatch. The balance-sheet identity (Assets = Liabilities + Equity) holds
# either way, so the merged row reconciles regardless of that taxonomy difference.
_CLASS_ORDER = ["Asset", "LiabEquity", "Income", "Expense"]
_CLASS_LABEL = {"Asset": "Assets", "LiabEquity": "Liabilities & Equity",
                "Income": "Income", "Expense": "Expense"}


def _class_of(root_type: str) -> str:
    return "LiabEquity" if root_type in ("Liability", "Equity") else root_type


def _to_float(v) -> float:
    try:
        return float(str(v or 0).replace(",", "").strip())
    except (ValueError, TypeError):
        return 0.0


def _signed(amount: float, dr_cr: str) -> float:
    """Dr-positive signed amount (so a receivable / asset is positive)."""
    return amount if dr_cr == "Dr" else -amount


def _side(net: float) -> dict:
    """{amount, dr_cr} for a Dr-positive signed net (0 -> blank side)."""
    if abs(net) < 0.005:
        return {"amount": 0.0, "dr_cr": ""}
    return {"amount": round(abs(net), 2), "dr_cr": "Dr" if net > 0 else "Cr"}


def _signed_of(side) -> float:
    if not side:
        return 0.0
    return _signed(side.get("amount", 0.0), side.get("dr_cr") or "Dr")


# ── Source side (pure) ────────────────────────────────────────────────────────

def source_totals(coa, masters) -> dict:
    """The opening trial balance as Tally states it (extracted masters + COA)."""
    by_class: dict[str, float] = {c: 0.0 for c in _CLASS_ORDER}
    for a in coa.accounts:
        if a.is_group or not a.opening_balance:
            continue
        # A Profit & Loss (Income/Expense) opening is never posted - ERPNext forbids it
        # in an Opening Entry (see OpeningBalanceImporter._account_lines). Its amount
        # therefore stays inside the Temporary Opening difference, so leave it out of the
        # class totals entirely: that folds it into the temporary_opening residual below,
        # matching what ERPNext actually holds, so the trial balance reconciles.
        if a.root_type in ("Income", "Expense"):
            continue
        cls = _class_of(a.root_type)
        by_class[cls] = by_class.get(cls, 0.0) + _signed(
            a.opening_balance, a.opening_dr_cr)

    cust_net = 0.0
    for c in masters.customers:
        amt, dc = TallyExtractor._parse_opening(c.get("OpeningBalance", ""))
        cust_net += _signed(amt, dc)
    supp_net = 0.0
    for s in masters.suppliers:
        amt, dc = TallyExtractor._parse_opening(s.get("OpeningBalance", ""))
        supp_net += _signed(amt, dc)

    stock_value = 0.0
    stock_items = 0
    for it in masters.items:
        qty = TallyExtractor._parse_quantity(it.get("OpeningBalance"))
        if qty <= 0:
            continue
        rate = TallyExtractor._parse_rate(it.get("OpeningRate"))
        if rate == 0:
            val = abs(_to_float(it.get("OpeningValue")))
            if val:
                rate = val / qty
        if rate == 0:
            rate = TallyExtractor._parse_rate(it.get("StandardCost"))
        stock_value += qty * rate
        stock_items += 1

    # Temporary Opening is the contra for every opening posting, so it ends holding
    # the negative of (ledger accounts + parties + stock) - exactly Tally's own
    # opening difference. With it included, the whole trial balance nets to zero.
    temp_net = -(sum(by_class.values()) + cust_net + supp_net + stock_value)
    return {
        "classes": [
            {"root": c, "label": _CLASS_LABEL[c], "side": _side(by_class[c])}
            for c in _CLASS_ORDER if abs(by_class[c]) >= 0.005
        ],
        "receivables": _side(cust_net),   # customers net Dr (Debtors control)
        "payables": _side(supp_net),      # suppliers net Cr (Creditors control)
        "stock": {"amount": round(stock_value, 2), "dr_cr": "Dr" if stock_value else ""},
        "stock_items": stock_items,
        "temporary_opening": _side(temp_net),
    }


# ── ERPNext side (Frappe, best-effort) ────────────────────────────────────────

def erpnext_totals(company: str, abbr: str) -> dict:
    """The opening trial balance as ERPNext holds it, read back from the GL + stock.

    Scoped to opening entries (``is_opening``) so later transactions don't pollute
    the figures. Returns ``{"available": False}`` on any error - the summary then
    shows Tally's side alone rather than failing.
    """
    import frappe

    try:
        rec_acc = frappe.get_cached_value("Company", company, "default_receivable_account")
        pay_acc = frappe.get_cached_value("Company", company, "default_payable_account")
        temp_acc = f"Temporary Opening - {abbr}"
        classes = _gl_by_class(company, {rec_acc, pay_acc, temp_acc})
        # Sum opening postings across ALL receivable / payable accounts, not just the
        # company defaults: forex party openings post to per-currency control accounts
        # ('Debtors USD', 'Creditors USD') created during the run, and those must be
        # included or the receivables/payables figure reads short by the forex total.
        rec_accs = frappe.get_all(
            "Account", filters={"company": company, "account_type": "Receivable",
                                "is_group": 0}, pluck="name") or ([rec_acc] if rec_acc else [])
        pay_accs = frappe.get_all(
            "Account", filters={"company": company, "account_type": "Payable",
                                "is_group": 0}, pluck="name") or ([pay_acc] if pay_acc else [])
        # Scope the control accounts to opening postings ONLY (this migration's opening
        # invoices + advance Payment Entries), never the account's full balance. The
        # tool supports re-runs and migrating into a company that may already hold
        # activity on Receivable/Payable/Stock; a full-balance read would fold that
        # pre-existing activity into the "ERPNext" column and show a false mismatch.
        receivables = _opening_account_balance(company, rec_accs) if rec_accs else None
        payables = _opening_account_balance(company, pay_accs) if pay_accs else None
        stock = {"amount": _opening_stock_value(company), "dr_cr": "Dr"}
        # Temporary Opening is the contra for every opening posting, so derive it as
        # the balancing residual of the scoped figures rather than reading the GL.
        # This keeps the ERPNext column internally balanced by construction and
        # immune to any non-opening activity, exactly mirroring how the source side
        # derives its own Temporary Opening (see source_totals).
        temp_signed = -(
            sum(_signed_of(s) for s in classes.values())
            + _signed_of(receivables) + _signed_of(payables) + _signed_of(stock))
        return {
            "available": True,
            "classes": classes,
            "receivables": receivables,
            "payables": payables,
            "stock": stock,
            "temporary_opening": _side(temp_signed),
        }
    except Exception:
        return {"available": False}


def _gl_by_class(company: str, exclude: set) -> dict:
    """Ledger account classes from the opening Journal Entry's GL, by merged class.

    Restricted to ``voucher_type='Journal Entry'`` opening entries (the opening JE),
    so party invoices and the stock reconciliation are excluded - those are reported
    on their own lines. The receivable / payable / Temporary Opening control accounts
    are excluded too. Liability and Equity are merged (see _class_of) so the capital
    taxonomy difference between Tally and ERPNext does not read as a mismatch."""
    import frappe

    rows = frappe.get_all(
        "GL Entry",
        filters={"company": company, "is_opening": "Yes", "is_cancelled": 0,
                 "voucher_type": "Journal Entry"},
        fields=["account", "debit", "credit"])
    nets: dict[str, float] = {}
    for r in rows:
        if r.account in exclude:
            continue
        root = frappe.get_cached_value("Account", r.account, "root_type") or "Asset"
        cls = _class_of(root)
        nets[cls] = nets.get(cls, 0.0) + (r.debit or 0) - (r.credit or 0)
    return {cls: _side(net) for cls, net in nets.items()}


def _opening_account_balance(company: str, accounts) -> dict:
    """{amount, dr_cr} net (Dr-positive) of just this migration's OPENING postings on
    one or more accounts - never the accounts' full balance.

    Accepts a single account name or a list (forex openings post to per-currency
    control accounts like 'Debtors USD' alongside the default, so all of them must
    be summed for the receivables/payables figure to reconcile).

    Two sources, matching what the importers post against a control account:
      * opening invoices - their GL is flagged ``is_opening='Yes'``;
      * advance Payment Entries - NOT flagged is_opening, but carry the
        ``PartyOpeningImporter`` remarks marker ('Tally opening: ...').
    Excluding everything else keeps a re-run, or a company with pre-existing
    activity on the account, from polluting the reconciliation figure."""
    import frappe

    account_filter = ["in", accounts] if isinstance(accounts, (list, tuple, set)) else accounts
    net = 0.0
    opening_rows = frappe.get_all(
        "GL Entry",
        filters={"company": company, "account": account_filter, "is_cancelled": 0,
                 "is_opening": "Yes"},
        fields=["debit", "credit"])
    net += sum((r.debit or 0) - (r.credit or 0) for r in opening_rows)
    advance_rows = frappe.get_all(
        "GL Entry",
        filters={"company": company, "account": account_filter, "is_cancelled": 0,
                 "voucher_type": "Payment Entry",
                 "remarks": ["like", "Tally opening:%"]},
        fields=["debit", "credit"])
    net += sum((r.debit or 0) - (r.credit or 0) for r in advance_rows)
    return _side(net)


def _opening_stock_value(company: str) -> float:
    """Value of just the opening Stock Reconciliation(s) this migration posted, read
    from their Stock Ledger Entries - not the warehouses' current stock value, which
    would include any movement posted after (or before) the migration."""
    import frappe

    recons = frappe.get_all(
        "Stock Reconciliation",
        filters={"company": company, "purpose": "Opening Stock", "docstatus": ["<", 2]},
        pluck="name")
    if not recons:
        return 0.0
    sles = frappe.get_all(
        "Stock Ledger Entry",
        filters={"voucher_type": "Stock Reconciliation", "voucher_no": ["in", recons],
                 "is_cancelled": 0},
        fields=["stock_value_difference"])
    return round(sum(r.stock_value_difference or 0 for r in sles), 2)


# ── Comparison ────────────────────────────────────────────────────────────────

def compare(source: dict, erp: dict) -> dict:
    """Assemble the trial-balance rows + a balanced Dr/Cr total + a verdict (no gate)."""
    available = bool(erp.get("available"))
    erp_classes = (erp.get("classes") or {}) if available else {}

    rows: list[dict] = []
    totals = {"src_dr": 0.0, "src_cr": 0.0, "erp_dr": 0.0, "erp_cr": 0.0}
    all_match = available

    def _accumulate(side, dr_key, cr_key):
        if not side:
            return
        if side.get("dr_cr") == "Dr":
            totals[dr_key] += side["amount"]
        elif side.get("dr_cr") == "Cr":
            totals[cr_key] += side["amount"]

    def add(key, label, src_side, erp_side, is_diff=False):
        nonlocal all_match
        has = erp_side is not None
        match = bool(has and abs(_signed_of(src_side) - _signed_of(erp_side)) < _TOLERANCE)
        if has and not match:
            all_match = False
        _accumulate(src_side, "src_dr", "src_cr")
        if has:
            _accumulate(erp_side, "erp_dr", "erp_cr")
        rows.append({
            "key": key, "label": label,
            "source": src_side, "erpnext": erp_side,
            "has_erpnext": has, "match": match,
            "is_opening_difference": is_diff,
        })

    for c in source["classes"]:
        add(f"class_{c['root']}", c["label"], c["side"],
            erp_classes.get(c["root"]) if available else None)
    add("receivables", "Receivables", source["receivables"],
        erp.get("receivables") if available else None)
    add("payables", "Payables", source["payables"],
        erp.get("payables") if available else None)
    add("stock", "Stock value", source["stock"],
        erp.get("stock") if available else None)
    add("temporary_opening", "Temporary Opening", source["temporary_opening"],
        erp.get("temporary_opening") if available else None, is_diff=True)

    verdict = "source_only" if not available else ("reconciled" if all_match else "review")
    return {
        "verdict": verdict,
        "available": available,
        "stock_items": source.get("stock_items", 0),
        "rows": rows,
        "total": {
            "source": {"dr": round(totals["src_dr"], 2), "cr": round(totals["src_cr"], 2)},
            "erpnext": {"dr": round(totals["erp_dr"], 2), "cr": round(totals["erp_cr"], 2)},
            "source_balanced": abs(totals["src_dr"] - totals["src_cr"]) < _TOLERANCE,
            "erpnext_balanced": available and abs(totals["erp_dr"] - totals["erp_cr"]) < _TOLERANCE,
        },
    }


def build_reconciliation(company: str, abbr: str, coa, masters) -> dict:
    """Compose the source trial balance, read the ERPNext side, and compare. Read-only."""
    return compare(source_totals(coa, masters), erpnext_totals(company, abbr))
