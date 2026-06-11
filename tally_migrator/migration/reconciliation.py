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

# ERPNext root types in trial-balance reading order, with display labels.
_ROOT_ORDER = ["Asset", "Liability", "Equity", "Income", "Expense"]
_ROOT_LABEL = {"Asset": "Assets", "Liability": "Liabilities", "Equity": "Equity",
               "Income": "Income", "Expense": "Expense"}


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
    by_root: dict[str, float] = {r: 0.0 for r in _ROOT_ORDER}
    for a in coa.accounts:
        if a.is_group or not a.opening_balance:
            continue
        by_root[a.root_type] = by_root.get(a.root_type, 0.0) + _signed(
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
    temp_net = -(sum(by_root.values()) + cust_net + supp_net + stock_value)
    return {
        "classes": [
            {"root": r, "label": _ROOT_LABEL[r], "side": _side(by_root[r])}
            for r in _ROOT_ORDER if abs(by_root[r]) >= 0.005
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
        return {
            "available": True,
            "classes": _gl_by_root(company, {rec_acc, pay_acc, temp_acc}),
            "receivables": _gl_net(company, rec_acc) if rec_acc else None,
            "payables": _gl_net(company, pay_acc) if pay_acc else None,
            "stock": {"amount": _stock_value(company), "dr_cr": "Dr"},
            "temporary_opening": _gl_net(company, temp_acc),
        }
    except Exception:
        return {"available": False}


def _gl_by_root(company: str, exclude: set) -> dict:
    """Ledger account classes from the opening Journal Entry's GL, by root_type.

    Restricted to ``voucher_type='Journal Entry'`` opening entries (the opening JE),
    so party invoices and the stock reconciliation are excluded - those are reported
    on their own lines. The receivable / payable / Temporary Opening control accounts
    are excluded too (also shown separately)."""
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
        nets[root] = nets.get(root, 0.0) + (r.debit or 0) - (r.credit or 0)
    return {root: _side(net) for root, net in nets.items()}


def _gl_net(company: str, account: str) -> dict:
    """{amount, dr_cr} net (Dr-positive) of an account's opening GL entries."""
    import frappe

    rows = frappe.get_all(
        "GL Entry",
        filters={"company": company, "account": account,
                 "is_opening": "Yes", "is_cancelled": 0},
        fields=["debit", "credit"])
    net = sum((r.debit or 0) - (r.credit or 0) for r in rows)
    return _side(net)


def _stock_value(company: str) -> float:
    import frappe

    whs = frappe.get_all(
        "Warehouse", filters={"company": company, "is_group": 0}, pluck="name")
    if not whs:
        return 0.0
    rows = frappe.get_all("Bin", filters={"warehouse": ["in", whs]}, fields=["stock_value"])
    return round(sum(r.stock_value or 0 for r in rows), 2)


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
