"""Opening balances: the per-company lock and the three opening importers."""

import contextlib
import sys
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
from tally_migrator.naming import (
    safe_item_code, company_scoped, shared_batch_names, batch_id_for)
from tally_migrator.tally.extractors import TallyExtractor
from tally_migrator.validation.engine import (
    infer_gst_category, validate_gstin, GSTIN_STATE_CODES,
)
from .base import BaseImporter, ImportResult
from .items import ItemImporter

# ── Concurrency guard for opening entries ─────────────────────────────────────
# The opening Journal Entry and Stock Reconciliation are aggregate, submitted
# documents guarded against re-posting by an existence check (see
# OpeningBalanceImporter._existing_opening_state). That check is read-then-write,
# so two runs of the SAME company racing each other could both read "none exist"
# and both post - doubling the opening trial balance. A short, self-expiring Redis
# lock per company serialises the check-and-post: only one run posts at a time, and
# the other then sees the now-committed entries (via the existence check) and stands
# down. Lock + existence check together close the window with no stale-lock risk.
_OPENING_LOCK_TTL = 3600   # seconds; ample to post, self-expiring if a worker dies


@contextlib.contextmanager
def _company_opening_lock(company: str):
    """Best-effort per-company lock around opening-balance/stock posting.

    Yields True when this process holds the lock, False when another run already
    does. Site-namespaced so it is safe on shared Redis. If the cache is
    unavailable the lock is treated as acquired - the DB-level existence check
    remains the correctness backstop, so a cache outage never blocks a real run.
    """
    site = getattr(frappe.local, "site", "") or ""
    key = f"tally_migrator:opening:{site}:{company}"
    cache = frappe.cache()
    acquired = False
    try:
        # redis SETNX + TTL: set only if absent, auto-expire so a dead worker can't
        # hold the lock forever.
        acquired = bool(cache.set(key, frappe.utils.now(), nx=True, ex=_OPENING_LOCK_TTL))
    except Exception:
        # Cache down / unavailable: proceed without the lock. The existence check
        # still prevents the common (sequential) double-post.
        yield True
        return
    try:
        yield acquired
    finally:
        if acquired:
            try:
                cache.delete(key)
            except Exception:
                pass




# ── Opening balance importer ─────────────────────────────────────────────────

class OpeningBalanceImporter:
    """Posts one balanced 'Opening Entry' Journal Entry for the whole trial balance.

    Three balance sources are combined into a single submitted JE:
      • ledger accounts  - Dr/Cr against the account itself,
      • customers        - against the company's default Receivable account, with
                           ``party_type='Customer'`` / ``party=<name>``,
      • suppliers        - against the default Payable account, ``party=<name>``.

    Referenced accounts/parties are normally created first (COA + Customers +
    Suppliers). A line whose account/party did *not* get created (its earlier import
    failed) is skipped with a warning rather than included - otherwise a single
    missing reference would make the whole submitted entry throw and roll back,
    silently dropping *every* opening balance. ERPNext requires the JE to balance;
    any residual difference
    (e.g. only part of the trial balance was migrated) is absorbed by a balancing
    line against 'Temporary Opening - <ABBR>'. The entry is **submitted** so the
    balances actually post to the General Ledger.
    """

    # Marker written to every batch's ``user_remark`` so a later re-run can tell
    # which batches this migrator already posted (per-batch idempotency) and skip
    # only those, posting the rest. Kept in one place so the writer (``_post_batch``)
    # and reader (``_existing_opening_state``) can never drift apart.
    _REMARK_PREFIX = "Opening balances imported from Tally ("

    def __init__(self, company: str, abbr: str):
        self.company = company
        self.abbr = abbr

    def run(self, accounts: list, customers: list, suppliers: list,
            posting_date: str) -> ImportResult:
        result = ImportResult("Journal Entry")
        # Idempotency: the opening JE is an aggregate document, not a per-record
        # upsert, so re-posting a batch would double the books. But posting is
        # per-batch (one JE per root type / party type), so the guard must be
        # per-batch too - otherwise a re-run after a *partial* failure (some
        # batches committed, one failed) would see the committed batches and skip
        # *everything*, silently abandoning the balances that never posted.
        #
        # ``foreign`` is True when an Opening Entry exists that this migrator did
        # NOT create (e.g. the company's books were opened manually); in that case
        # we stay conservative and skip entirely rather than risk double-posting
        # against a hand-built opening trial balance.
        foreign, posted = self._existing_opening_state()
        if foreign:
            result.skipped += 1
            result.add_warning(
                "Opening Entry",
                "this company already has an Opening Entry that was not created by "
                "this migrator - opening balances were skipped to avoid double-posting "
                "against a manually set-up book. Cancel that entry first if you want "
                "this migration to post the opening balances instead.")
            return result

        # Build the entry in batches (one per root type, plus Customers and
        # Suppliers) rather than a single all-or-nothing document. A validation
        # failure on submit then loses only its own batch, not the whole trial
        # balance, and each batch commits independently. Every batch is balanced
        # against 'Temporary Opening', so the batches' plugs net to exactly the
        # same Temporary Opening balance a single combined entry would produce.
        batches: list[tuple[str, list[dict]]] = []
        for root_type in ("Asset", "Liability", "Equity", "Income", "Expense"):
            rows = self._account_lines(
                [a for a in accounts if a.root_type == root_type], result)
            if rows:
                batches.append((root_type, rows))
        cust = self._party_lines(customers, "Customer", "default_receivable_account", result)
        if cust:
            batches.append(("Customer", cust))
        supp = self._party_lines(suppliers, "Supplier", "default_payable_account", result)
        if supp:
            batches.append(("Supplier", supp))
        if not batches:
            return result  # nothing to post

        # Drop batches a prior run already posted (per-batch idempotency); post the
        # rest. This is what makes "fix it and re-run" actually complete a partially
        # posted opening trial balance instead of skipping it wholesale.
        pending: list[tuple[str, list[dict]]] = []
        for label, lines in batches:
            if label in posted:
                result.skipped += 1
                result.add_warning(
                    label,
                    f"opening balances for {label} were already posted in an earlier "
                    "run - skipped to avoid double-posting. The remaining batches (if "
                    "any) were posted now.")
                continue
            pending.append((label, lines))
        if not pending:
            return result  # every batch already posted

        # The 'did not net to zero' check is meaningful only across the balances we
        # are actually posting now - per-batch plugs are expected and large - so warn
        # once here over the pending batches, then let each batch plug silently.
        self._warn_residual([l for _, rows in pending for l in rows], result)
        for label, lines in pending:
            self._post_batch(label, lines, posting_date, result)
        return result

    def _post_batch(self, label: str, lines: list[dict], posting_date: str,
                    result: ImportResult) -> None:
        """Insert + submit one Opening Entry batch, committing on its own.

        Plugs the batch against Temporary Opening (silently - the aggregate
        residual is warned once by the caller). A failure rolls back only this
        batch's transaction; batches already committed are unaffected."""
        self._balance(lines)
        try:
            doc = frappe.get_doc({
                "doctype": "Journal Entry",
                "voucher_type": "Opening Entry",
                "posting_date": posting_date,
                "company": self.company,
                "accounts": lines,
                "user_remark": f"{self._REMARK_PREFIX}{label})",
            })
            doc.insert(ignore_permissions=True)
            # Submit synchronously. ERPNext's JournalEntry.submit() enqueues the
            # submission as a BACKGROUND job when the entry has > 100 accounts
            # (the asset Opening Entry routinely does), returning before the GL is
            # posted. The single migration worker is then busy until the run ends,
            # so that queued submit - and its GL - only lands AFTER finalize, which
            # makes the post-import trial balance read a whole account class as
            # absent. _submit() is exactly the path submit() takes for <=100-row
            # entries, so this forces the same synchronous posting for any size.
            # Safe here because the migration already runs off the web request,
            # so there is no request-timeout reason to defer.
            doc._submit()
            frappe.db.commit()
            result.add_created(doc.name)
        except Exception as exc:
            result.add_error(f"Opening Entry ({label})", exc)
            frappe.db.rollback()

    def _existing_opening_state(self) -> tuple[bool, set[str]]:
        """Inspect this company's non-cancelled Opening Entries.

        Returns ``(foreign, posted)``:
        - ``posted`` - the set of batch labels this migrator already posted,
          recovered from each entry's ``user_remark`` marker, so a re-run can skip
          exactly those batches and post the rest.
        - ``foreign`` - True when an Opening Entry exists whose remark this migrator
          did *not* write (opening balances set up by hand or another tool); the
          caller then skips entirely rather than double-post against those books.
        """
        remarks = frappe.get_all(
            "Journal Entry",
            filters={
                "company": self.company,
                "voucher_type": "Opening Entry",
                "docstatus": ["<", 2],   # draft or submitted, not cancelled
            },
            pluck="user_remark",
        )
        posted: set[str] = set()
        foreign = False
        for remark in remarks or []:
            label = self._batch_label_from_remark(remark)
            if label:
                posted.add(label)
            else:
                foreign = True
        return foreign, posted

    @classmethod
    def _batch_label_from_remark(cls, remark) -> str:
        """Recover the batch label from a ``user_remark`` we wrote, else "".

        ``"Opening balances imported from Tally (Asset)"`` → ``"Asset"``; anything
        that doesn't match our marker (a hand-written or third-party entry) → "".
        """
        s = (remark or "").strip()
        if s.startswith(cls._REMARK_PREFIX) and s.endswith(")"):
            return s[len(cls._REMARK_PREFIX):-1].strip()
        return ""

    # ── Line builders ────────────────────────────────────────────────────────
    def _account_lines(self, accounts: list, result: ImportResult) -> list[dict]:
        lines = []
        for node in accounts:
            if not node.opening_balance or node.is_group:
                continue
            # ERPNext forbids a Profit & Loss (Income/Expense) account from carrying an
            # opening balance - GL Entry.check_pl_account throws "'Profit and Loss' type
            # account ... not allowed in Opening Entry" on submit, failing the whole
            # batch. This happens in a mid-year migration where a Tally income/expense
            # ledger carried a year-to-date balance. We cannot post it, so skip the line
            # with a clear note. The amount is not lost: every other batch plugs against
            # Temporary Opening, so it stays inside that difference, to be cleared when
            # the user completes their opening entries. (reconciliation.source_totals
            # folds these into Temporary Opening too, so the trial balance reconciles.)
            if node.root_type in ("Income", "Expense"):
                result.add_warning(
                    node.name,
                    "This is an income or expense account, which ERPNext does not allow "
                    f"to carry an opening balance, so {abs(node.opening_balance):,.2f} was "
                    "not posted. It is included in the Temporary Opening total instead.")
                continue
            account = company_scoped(node.name, self.abbr)
            is_group = frappe.db.get_value("Account", account, "is_group")
            if is_group is None:
                result.add_warning(
                    node.name,
                    f"opening balance skipped - account '{account}' was not created "
                    "(its import failed earlier). Fix the account and re-run.")
                continue
            # The Tally ledger exists in ERPNext as a GROUP account - it had
            # sub-accounts under it in Tally, so it was created (or promoted) as a
            # group, and ERPNext forbids a group account from carrying a balance
            # ("group accounts cannot be used in transactions"). Including it would
            # fail the whole class's Opening Entry on submit and lose every balance in
            # that batch, so skip just this line. The amount is not lost: the batch
            # plugs against Temporary Opening, so it stays inside that difference, to
            # be cleared when the user completes their opening entries - the same
            # contract as an income/expense opening above.
            if is_group:
                result.add_warning(
                    node.name,
                    f"opening balance skipped - account '{account}' exists as a group "
                    "account (its Tally ledger had sub-accounts), and ERPNext does not "
                    f"allow a group account to carry a balance, so {abs(node.opening_balance):,.2f} "
                    "was not posted. It is included in the Temporary Opening total instead.")
                continue
            lines.append(self._line(account, node.opening_balance, node.opening_dr_cr))
        return lines

    # Tally party name → the field ERPNext stores it under; the document's own
    # ``name`` can differ (e.g. a naming series), so we resolve it rather than
    # assuming ``name == display name``.
    _PARTY_KEY_FIELD = {"Customer": "customer_name", "Supplier": "supplier_name"}

    def _party_lines(self, parties: list, party_type: str, company_field: str,
                     result: ImportResult) -> list[dict]:
        if not parties:
            return []
        control = frappe.get_cached_value("Company", self.company, company_field)
        key_field = self._PARTY_KEY_FIELD[party_type]
        lines, missing_control = [], False
        for record in parties:
            amount, drcr = TallyExtractor._parse_opening(record.get("OpeningBalance", ""))
            if not amount:
                continue
            if not control:
                missing_control = True
                continue
            # Resolve the actual document name (handles naming series and any
            # existing party matched on display name but stored under another id).
            # A missing name means the party wasn't created - skip with a warning
            # rather than posting against, or failing the whole entry on, a bad id.
            party_name = frappe.db.get_value(party_type, {key_field: record["_name"]}, "name")
            if not party_name:
                result.add_warning(
                    record["_name"],
                    f"opening balance skipped - {party_type} '{record['_name']}' was "
                    "not created (its import failed earlier). Fix it and re-run.")
                continue
            line = self._line(control, amount, drcr)
            line.update({"party_type": party_type, "party": party_name})
            lines.append(line)
        if missing_control:
            result.add_error(
                f"{party_type} opening balances",
                f"company has no {company_field.replace('_', ' ')} set - skipped",
            )
        return lines

    @staticmethod
    def _line(account: str, amount: float, drcr: str) -> dict:
        """A JE line; Dr (or blank) → debit, Cr → credit."""
        if drcr == "Cr":
            return {"account": account, "debit_in_account_currency": 0.0,
                    "credit_in_account_currency": amount}
        return {"account": account, "debit_in_account_currency": amount,
                "credit_in_account_currency": 0.0}

    # A residual below this (currency units) is rounding noise, not a real gap.
    PLUG_NOISE_THRESHOLD = 1.0

    def _balance(self, lines: list[dict], result: "ImportResult | None" = None) -> None:
        # A non-trivial residual means the migrated balances don't net to zero on
        # their own (usually only part of the trial balance was migrated). When a
        # result is given, surface that gap before plugging it; the batch path warns
        # once on the aggregate instead and calls this with no result.
        if result is not None:
            self._warn_residual(lines, result)
        total_dr = sum(l["debit_in_account_currency"] for l in lines)
        total_cr = sum(l["credit_in_account_currency"] for l in lines)
        diff = round(total_dr - total_cr, 2)
        if diff == 0:
            return
        temp = company_scoped("Temporary Opening", self.abbr)
        if diff > 0:
            lines.append({"account": temp, "debit_in_account_currency": 0.0,
                          "credit_in_account_currency": diff})
        else:
            lines.append({"account": temp, "debit_in_account_currency": abs(diff),
                          "credit_in_account_currency": 0.0})

    def _warn_residual(self, lines: list[dict], result: ImportResult) -> None:
        """Flag a non-fatal warning when the lines don't net to zero (a Temporary
        Opening plug will absorb the gap, so it must not be hidden)."""
        diff = round(sum(l["debit_in_account_currency"] for l in lines)
                     - sum(l["credit_in_account_currency"] for l in lines), 2)
        if abs(diff) < self.PLUG_NOISE_THRESHOLD:
            return
        result.add_warning(
            "Opening Entry",
            f"{abs(diff):,.2f} is held in 'Temporary Opening - {self.abbr}' to keep your "
            "books balanced. It is the part of your Tally opening that does not balance "
            "on its own, plus any income/expense opening balances ERPNext cannot carry. "
            "This is normal - clear it as you finish your opening entries.")


# ── Party opening balances (invoice-wise) ────────────────────────────────────

class PartyOpeningImporter:
    """Posts party (customer/supplier) opening balances *invoice-wise*.

    The lump-sum JE line per party (the old ``OpeningBalanceImporter`` party path)
    is trial-balance correct but destroys bill-level traceability: every future
    payment reconciles against a single lump, so aged debtors/creditors and
    invoice-by-invoice matching are lost. This importer instead reproduces each
    outstanding bill Tally carries in ``BILLALLOCATIONS.LIST`` (extracted as
    :class:`~tally_migrator.tally.extractors.BillAllocation`) as a real ERPNext
    opening document:

    - **Outstanding bill** (on the party's natural side) -> a one-line opening
      Sales/Purchase Invoice (``is_opening='Yes'``), contra'd to Temporary Opening
      exactly like ERPNext's own Opening Invoice Creation Tool. Future Payment
      Entries then reconcile against it individually.
    - **Advance / credit balance** (Tally's ISADVANCE flag, or a bill on the side
      opposite the party's natural side) -> an opening Payment Entry, left
      unallocated so it is ready to apply against a later invoice.
    - **No bill detail** (party opening with an empty BILLALLOCATIONS list) -> a
      single lump opening invoice for the whole ledger opening (chosen fallback),
      so the party is still reconcilable as one outstanding rather than a JE line.
    - **Bills do not reconcile to the ledger opening** -> the bills post as-is and
      the residual (ledger opening minus bills) posts as one "On Account" plug on
      the natural side, with a warning, so the party's net still ties to the
      ledger figure (and thus the trial balance).

    Non-party balances (cash, assets, P&L) stay with ``OpeningBalanceImporter``;
    the two compose in the orchestrator. Idempotency: every document carries a
    marker in ``remarks`` (``party | bill``); a re-run reads the markers already
    posted and skips exactly those, so "fix and re-run" fills gaps without
    double-posting.

    The Tally bill id (BILLALLOCATIONS.LIST/NAME) names the ERPNext opening invoice
    (``insert(set_name=...)``), exactly like ERPNext's Opening Invoice Creation Tool's
    "invoice number from the previous system", so the document id reconciles directly
    against the Tally invoice. For Purchase it is also kept in ``bill_no`` (Supplier
    Invoice No). A bill id that collides with an existing document falls back to
    auto-naming with a warning, so an opening is never lost to an id clash.

    Posting date is the migration opening date (not the original bill date): an
    opening invoice backdated into a year with no Fiscal Year record would be
    rejected, so the original bill date is preserved in ``bill_date`` (Purchase) and
    the remarks marker. Single-currency only (v1); a foreign-currency party is
    handled by the ledger-level fallback upstream.
    """

    _MARKER = "Tally opening"          # remarks prefix → idempotency key
    _PLUG_THRESHOLD = 1.0              # a residual below this is rounding noise
    _PARTY_KEY_FIELD = {"Customer": "customer_name", "Supplier": "supplier_name"}

    def __init__(self, company: str, abbr: str, posting_date: str):
        self.company = company
        self.abbr = abbr
        self.posting_date = posting_date

    # ── Orchestration ─────────────────────────────────────────────────────────
    def run(self, bills: list, customers: list[dict],
            suppliers: list[dict], on_progress=None) -> ImportResult:
        result = ImportResult("Opening Invoice")
        by_party: dict[str, list] = {}
        for b in bills or []:
            by_party.setdefault(b.party, []).append(b)
        seen = self._existing_markers()
        # Live progress across both party lists: this is the heavy phase (one
        # submitted invoice per bill), so the bar must move within it. ``tick`` fires
        # once per party - posted or skipped - so ``done`` reaches ``total``.
        total = len(customers or []) + len(suppliers or [])
        progress = {"done": 0}

        def tick():
            progress["done"] += 1
            if on_progress:
                on_progress(progress["done"], total)

        self._process(customers, "Customer", by_party, seen, result, tick)
        self._process(suppliers, "Supplier", by_party, seen, result, tick)
        return result

    def _process(self, parties: list[dict], party_type: str,
                 by_party: dict, seen: set, result: ImportResult,
                 tick=None) -> None:
        if not parties:
            return
        key_field = self._PARTY_KEY_FIELD[party_type]
        for record in parties:
            if tick:
                tick()
            tally_name = record["_name"]
            ledger_amt, ledger_drcr = TallyExtractor._parse_opening(
                record.get("OpeningBalance", ""))
            party_bills = by_party.get(tally_name, [])
            if not ledger_amt and not party_bills:
                continue  # nothing to post for this party
            party = frappe.db.get_value(party_type, {key_field: tally_name}, "name")
            if not party:
                result.add_warning(
                    tally_name,
                    f"opening balance skipped - {party_type} '{tally_name}' was not "
                    "created (its import failed earlier). Fix it and re-run.")
                continue

            # Forex party: post in its own currency (Option B - true multi-currency
            # AR/AP). CurrencyISO is set in extraction only when the opening balance
            # carried a foreign currency symbol that resolved to an ISO (Tally exports
            # no CurrencyName tag on the party ledger), so its presence is the forex
            # signal. We also honour an already-created party that carries a
            # non-company default currency. Routed to a dedicated path that posts one
            # opening invoice in the foreign currency against a currency-denominated
            # receivable/payable account, so the outstanding tracks the foreign amount
            # and the base reconciles.
            if (record.get("CurrencyISO")
                    or self._is_foreign_currency_party(party_type, party)):
                self._emit_forex(party_type, party, tally_name, record,
                                 party_bills, seen, result)
                continue

            ledger_signed = self._signed(ledger_amt, ledger_drcr)
            bills_signed = 0.0
            for b in party_bills:
                bills_signed += self._signed(b.amount, b.dr_cr)
                # real_bill: this carries a genuine Tally bill reference (not the lump
                # 'Opening'/'On Account' plug below), so the opening invoice is named
                # after it for direct reconciliation against the Tally invoice id.
                self._emit(party_type, party, tally_name, self._signed(b.amount, b.dr_cr),
                           b.bill_no, b.bill_date, b.is_advance, seen, result,
                           real_bill=True)

            residual = round(ledger_signed - bills_signed, 2)
            if abs(residual) >= self._PLUG_THRESHOLD:
                # No bills at all -> this *is* the party's opening (normal lump, no
                # warning). Bills present but short -> a real mismatch (warn).
                if party_bills:
                    result.add_warning(
                        tally_name,
                        f"bill-wise openings for '{tally_name}' did not add up to its "
                        f"ledger opening; {abs(residual):,.2f} was posted as an "
                        "'On Account' opening to match the ledger. Review the party's "
                        "outstanding bills in Tally.")
                label = "On Account" if party_bills else "Opening"
                self._emit(party_type, party, tally_name, residual,
                           label, self.posting_date, False, seen, result)

    # ── Per-bill emission ─────────────────────────────────────────────────────
    def _emit(self, party_type: str, party: str, tally_name: str, signed: float,
              bill_no: str, bill_date: str, is_advance: bool,
              seen: set, result: ImportResult, real_bill: bool = False) -> None:
        amount = abs(signed)
        if amount < self._PLUG_THRESHOLD:
            return
        marker = f"{self._MARKER}: {tally_name} | {bill_no}"
        if marker in seen:
            result.skipped += 1
            return
        kind = self._classify(party_type, signed, is_advance)
        try:
            if kind == "advance":
                data = self._advance_dict(party_type, party, amount, bill_no, marker)
                set_name = None
            else:
                data = self._invoice_dict(
                    party_type, party, amount, bill_no, bill_date, marker)
                # Name the opening invoice after the Tally bill id, exactly like
                # ERPNext's own Opening Invoice Creation Tool (invoice_number ->
                # set_name): the ERPNext document id then IS the previous-system
                # invoice id, so a future payment / report reconciles against the
                # Tally invoice directly. Only for a genuine bill reference, never the
                # 'Opening'/'On Account' lump plug.
                set_name = bill_no if real_bill else None
            doc = self._insert_invoice(data, set_name, tally_name, bill_no, result)
            doc.submit()
            frappe.db.commit()
            seen.add(marker)
            # Real doctype (Sales/Purchase Invoice or Payment Entry), not the
            # "Opening Invoice" label, so the log can deep-link each document.
            result.add_created(doc.name, doc.doctype)
        except Exception as exc:
            frappe.db.rollback()
            result.add_error(f"{tally_name} | {bill_no}", exc)

    # ── Foreign-currency openings (Option B: true multi-currency AR/AP) ────────
    def _emit_forex(self, party_type: str, party: str, tally_name: str, record: dict,
                    party_bills: list, seen: set, result: ImportResult) -> None:
        """Post a forex party's opening as foreign-currency opening invoice(s) against
        a currency-denominated receivable/payable account.

        Bill-wise when the export carries per-bill foreign amounts: one named invoice
        per Tally bill (mirroring the base-currency path, so the ERPNext invoice id IS
        the Tally bill id and reconciles individually). Otherwise a single consolidated
        invoice for the whole ledger opening. Foreign-currency advances/credits are not
        migrated (flagged for manual entry)."""
        iso = (record.get("CurrencyISO") or "").strip()
        if not iso or iso == self._company_currency():
            result.add_warning(
                tally_name,
                f"opening balance skipped - couldn't resolve the foreign currency for "
                f"'{tally_name}'; enter its opening manually so the rate is correct.")
            return
        foreign, _sym, base, drcr = TallyExtractor._parse_forex_opening(
            record.get("OpeningBalance", ""))
        if not foreign or not base:
            result.add_warning(
                tally_name,
                f"opening balance skipped - couldn't read the {iso} opening for "
                f"'{tally_name}'.")
            return
        ledger_signed = self._signed(base, drcr)
        if self._classify(party_type, ledger_signed, is_advance=False) != "invoice":
            result.add_warning(
                tally_name,
                f"opening balance skipped - '{tally_name}' is a {iso} advance (credit "
                "side); enter it manually (foreign-currency advances are not migrated "
                "in this version).")
            return
        account = self._ensure_currency_account(party_type, iso, result)
        frappe.db.set_value(party_type, party, "default_currency", iso,
                            update_modified=False)
        # Ledger rate, derived from Tally's stated base (not the '@ rate' clause) so the
        # ERPNext base equals Tally's base exactly even if they disagree by rounding.
        rate = base / foreign

        # Bills that carry a parseable foreign amount and sit on the party's natural
        # side become per-bill invoices; a forex advance/credit bill is not migrated.
        invoice_bills: list = []
        for b in party_bills:
            if not b.foreign_amount:
                continue
            if self._classify(party_type, self._signed(b.amount, b.dr_cr),
                              b.is_advance) != "invoice":
                result.add_warning(
                    tally_name,
                    f"{iso} advance/credit bill '{b.bill_no}' for '{tally_name}' not "
                    "migrated (foreign-currency advances are not supported in this "
                    "version); enter it manually.")
                continue
            invoice_bills.append(b)

        if invoice_bills:
            posted_base = 0.0
            for b in invoice_bills:
                # Per-bill rate from its own foreign/base, so each invoice's base ties
                # to Tally's bill amount exactly; falls back to the ledger rate.
                b_rate = (b.amount / b.foreign_amount) if b.foreign_amount else rate
                self._post_forex_invoice(
                    party_type, party, tally_name, b.foreign_amount, iso, b_rate,
                    account, f"{self._MARKER}: {tally_name} | {b.bill_no}",
                    b.bill_no, seen, result)
                posted_base += self._signed(b.amount, b.dr_cr)
            # A remainder on the natural side (bills short of the ledger opening) posts
            # as one 'Opening' invoice, so the party still ties to its ledger figure.
            residual = round(ledger_signed - posted_base, 2)
            if (abs(residual) >= self._PLUG_THRESHOLD
                    and self._classify(party_type, residual, False) == "invoice"):
                self._post_forex_invoice(
                    party_type, party, tally_name, round(abs(residual) / rate, 2), iso,
                    rate, account, f"{self._MARKER}: {tally_name} | Opening", None,
                    seen, result)
                result.add_warning(
                    tally_name,
                    f"bill-wise {iso} openings for '{tally_name}' did not add up to its "
                    f"ledger opening; the remainder was posted as one 'Opening' invoice.")
            return

        # No parseable per-bill foreign detail: one consolidated invoice for the whole
        # opening (the only faithful option - bill references without amounts can't be
        # split). Flag the collapse so the loss of bill-level detail is visible.
        if party_bills:
            result.add_warning(
                tally_name,
                f"'{tally_name}' carries bill references in {iso} without per-bill "
                "amounts; posted as a single opening invoice.")
        self._post_forex_invoice(
            party_type, party, tally_name, foreign, iso, rate, account,
            f"{self._MARKER}: {tally_name} | Opening", None, seen, result)

    def _post_forex_invoice(self, party_type: str, party: str, tally_name: str,
                            amount: float, iso: str, rate: float, account: str,
                            marker: str, set_name, seen: set,
                            result: ImportResult) -> None:
        """Insert + submit one foreign-currency opening invoice (one bill, or the lump).

        Routed through ``_insert_invoice`` so a per-bill invoice is named after the
        Tally bill id (and falls back to auto-naming on an id clash), exactly like the
        base-currency path. Idempotent on the remarks marker."""
        if marker in seen:
            result.skipped += 1
            return
        try:
            data = self._forex_invoice_dict(party_type, party, amount, iso, rate,
                                            account, marker, bill_no=set_name or "Opening")
            doc = self._insert_invoice(data, set_name, tally_name,
                                       set_name or "Opening", result)
            doc.submit()
            frappe.db.commit()
            seen.add(marker)
            result.add_created(doc.name, doc.doctype)
        except Exception as exc:
            frappe.db.rollback()
            result.add_error(f"{tally_name} | {set_name or 'Opening'} ({iso})", exc)

    def _ensure_currency_account(self, party_type: str, iso: str,
                                 result: "ImportResult | None" = None) -> str:
        """The company's receivable/payable account in ``iso`` (e.g. 'Debtors USD'),
        created once under the default control account's parent. ERPNext requires the
        party account currency to match a foreign invoice currency, so each foreign
        currency gets its own leaf account."""
        label = "Debtors" if party_type == "Customer" else "Creditors"
        name = company_scoped(f"{label} {iso}", self.abbr)
        if frappe.db.exists("Account", name):
            return name
        parent = frappe.db.get_value("Account", self._party_account(party_type),
                                     "parent_account")
        acc = frappe.get_doc({
            "doctype": "Account", "account_name": f"{label} {iso}",
            "parent_account": parent, "company": self.company,
            "account_type": "Receivable" if party_type == "Customer" else "Payable",
            "account_currency": iso, "is_group": 0,
        })
        acc.insert(ignore_permissions=True)
        frappe.db.commit()
        # Record the account this run creates in the migration manifest, so an undo
        # can remove it too. Created before the forex invoice that uses it, so within
        # the Party Openings batch it is deleted after that invoice (reverse order).
        if result is not None:
            result.add_created(acc.name, "Account")
        return acc.name

    def _forex_invoice_dict(self, party_type: str, party: str, amount: float,
                            iso: str, rate: float, account: str, marker: str,
                            bill_no: str = "Opening") -> dict:
        is_sales = party_type == "Customer"
        item = {
            "item_name": "Opening", "description": "Opening balance",
            "uom": self._stock_uom(), "conversion_factor": 1.0, "qty": 1,
            "rate": amount,
            ("income_account" if is_sales else "expense_account"): self._temp_account(),
            "cost_center": self._cost_center(),
        }
        data = {
            "doctype": "Sales Invoice" if is_sales else "Purchase Invoice",
            "company": self.company, "is_opening": "Yes", "set_posting_time": 1,
            "posting_date": self.posting_date, "due_date": self.posting_date,
            frappe.scrub(party_type): party, "items": [item],
            "currency": iso, "conversion_rate": rate,
            ("debit_to" if is_sales else "credit_to"): account,
            "update_stock": 0, "disable_rounded_total": 1, "remarks": marker,
        }
        if not is_sales:
            data["bill_no"] = bill_no
            data["bill_date"] = self.posting_date
        return data

    def _insert_invoice(self, data: dict, set_name, tally_name: str, bill_no: str,
                        result: ImportResult):
        """Insert the opening document, naming it after the Tally bill id when given.

        A bill id can collide across parties (two ledgers each carrying a 'Bill 1'),
        and ERPNext document ids are global, so a clash would otherwise fail the whole
        opening. On a duplicate we fall back to auto-naming so the opening still posts
        - the Tally id stays recoverable (Purchase 'Supplier Invoice No' + the remarks
        marker) - and flag that it could not be used as the document id."""
        doc = frappe.get_doc(data)
        doc.flags.ignore_mandatory = True
        if not set_name:
            doc.insert(ignore_permissions=True)
            return doc
        try:
            doc.insert(ignore_permissions=True, set_name=set_name)
            return doc
        except frappe.DuplicateEntryError:
            frappe.db.rollback()
            result.add_warning(
                tally_name,
                f"opening invoice could not use the Tally bill id '{bill_no}' as its "
                "ERPNext id (already in use); it was auto-named instead. Reconcile via "
                "the Supplier Invoice No / remarks.")
            doc = frappe.get_doc(data)
            doc.flags.ignore_mandatory = True
            doc.insert(ignore_permissions=True)
            return doc

    @staticmethod
    def _classify(party_type: str, signed: float, is_advance: bool) -> str:
        """Outstanding invoice vs advance/credit, decided PURELY by which side of the
        party's control account the bill sits on. Customer natural side = Dr
        (signed > 0); Supplier = Cr (signed < 0): a bill there is an outstanding
        opening invoice. A bill on the OPPOSITE side is an advance/credit and posts
        as an unallocated Payment Entry.

        Tally's ISADVANCE flag is deliberately NOT used to force 'advance'. A genuine
        advance already falls on the opposite side (a customer prepayment is a credit,
        a supplier prepayment is a debit), so side alone routes it correctly. But some
        exports also tag a bill that sits on the party's NATURAL side as ISADVANCE
        (e.g. a supplier credit bill that offsets a debit bill). Forcing those to an
        advance posted them on the WRONG side via an opposite-direction Payment Entry,
        flipping their sign - so an offsetting Cr+Dr pair that nets to zero in Tally
        posted as a non-zero party balance (understating Payables/Receivables). The
        advance Payment Entry's direction is fixed by party type (supplier=Pay/debit,
        customer=Receive/credit), so it can only carry a bill that is genuinely on the
        opposite side; routing by side keeps every bill's GL effect equal to Tally's.
        ``is_advance`` is retained in the signature (it is a real Tally attribute the
        caller passes through) but does not change the side a balance lands on."""
        on_natural_side = (signed > 0) if party_type == "Customer" else (signed < 0)
        return "invoice" if on_natural_side else "advance"

    @staticmethod
    def _signed(amount: float, dr_cr: str) -> float:
        """Dr-positive signed amount (so a customer receivable is positive)."""
        return amount if dr_cr == "Dr" else -amount

    # ── Document builders (pure given the resolved company context) ────────────
    def _invoice_dict(self, party_type: str, party: str, amount: float,
                      bill_no: str, bill_date: str, marker: str) -> dict:
        is_sales = party_type == "Customer"
        item = {
            "item_name": f"Opening - {bill_no}"[:140],
            "description": f"Opening balance ({bill_no})",
            "uom": self._stock_uom(),
            "conversion_factor": 1.0,
            "qty": 1,
            "rate": amount,
            ("income_account" if is_sales else "expense_account"): self._temp_account(),
            "cost_center": self._cost_center(),
        }
        data = {
            "doctype": "Sales Invoice" if is_sales else "Purchase Invoice",
            "company": self.company,
            "is_opening": "Yes",
            "set_posting_time": 1,
            "posting_date": self.posting_date,
            "due_date": self.posting_date,
            frappe.scrub(party_type): party,
            "items": [item],
            "update_stock": 0,
            "disable_rounded_total": 1,
            "remarks": marker,
        }
        if not is_sales:
            # Purchase Invoice has native supplier-bill fields - preserve the real
            # Tally bill reference and date here (Sales Invoice has none, so the
            # marker in remarks is the only carrier there).
            data["bill_no"] = bill_no
            bd = bill_date or self.posting_date
            data["bill_date"] = bd
            # A Purchase Invoice's due date can never precede its supplier-invoice
            # (bill) date - ERPNext rejects it ("Due Date cannot be before Supplier
            # Invoice Date"). The opening posting_date is normally >= the bill date,
            # but real Tally data can carry a bill dated after the opening date, so
            # clamp the due date up to the bill date when that happens.
            try:
                from frappe.utils import getdate
                if getdate(bd) > getdate(self.posting_date):
                    data["due_date"] = bd
            except Exception:
                pass
        return data

    def _advance_dict(self, party_type: str, party: str, amount: float,
                      bill_no: str, marker: str) -> dict:
        """An opening advance as an unallocated Payment Entry. A customer advance is
        money Received (Debtors -> Temporary Opening); a supplier advance is money
        Paid (Temporary Opening -> Creditors). No references, so the whole amount
        sits as an advance ready to apply against a future invoice."""
        is_customer = party_type == "Customer"
        party_account = self._party_account(party_type)
        temp = self._temp_account()
        currency = self._company_currency()
        data = {
            "doctype": "Payment Entry",
            "payment_type": "Receive" if is_customer else "Pay",
            "company": self.company,
            "posting_date": self.posting_date,
            "party_type": party_type,
            "party": party,
            "paid_amount": amount,
            "received_amount": amount,
            "paid_from": party_account if is_customer else temp,
            "paid_to": temp if is_customer else party_account,
            "paid_from_account_currency": currency,
            "paid_to_account_currency": currency,
            # Payment Entry.set_remarks() OVERWRITES remarks with an auto-generated
            # string on save - which silently wiped our idempotency marker, so a re-run
            # could not detect the advance and re-created it (doubling Debtors/Creditors
            # advances). custom_remarks=1 tells ERPNext to keep our remarks verbatim, so
            # the marker survives for both the re-run guard (_existing_markers) and the
            # reconciliation (_opening_account_balance), exactly like the invoice path.
            "custom_remarks": 1,
            "remarks": marker,
            "reference_no": bill_no,
            "reference_date": self.posting_date,
        }
        return data

    # ── Resolved-once company context ──────────────────────────────────────────
    def _temp_account(self) -> str:
        if not hasattr(self, "_temp"):
            self._temp = company_scoped("Temporary Opening", self.abbr)
        return self._temp

    def _cost_center(self) -> str:
        if not hasattr(self, "_cc"):
            self._cc = frappe.get_cached_value("Company", self.company, "cost_center")
        return self._cc

    def _company_currency(self) -> str:
        if not hasattr(self, "_cur"):
            self._cur = frappe.get_cached_value(
                "Company", self.company, "default_currency") or "INR"
        return self._cur

    def _stock_uom(self) -> str:
        if not hasattr(self, "_uom"):
            self._uom = frappe.db.get_single_value(
                "Stock Settings", "stock_uom") or "Nos"
        return self._uom

    def _party_account(self, party_type: str) -> str:
        field = ("default_receivable_account" if party_type == "Customer"
                 else "default_payable_account")
        return frappe.get_cached_value("Company", self.company, field)


    def _is_foreign_currency_party(self, party_type: str, party: str) -> bool:
        """True when the party's own default currency is set and differs from the
        company currency. Both Customer and Supplier expose ``default_currency``;
        a blank value means 'follow the company', i.e. not foreign."""
        cur = frappe.db.get_value(party_type, party, "default_currency")
        return bool(cur) and cur != self._company_currency()

    def _existing_markers(self) -> set:
        """Markers already posted for this company, so a re-run skips exactly the
        bills it has posted before and fills only the gaps. Reads opening Sales/
        Purchase Invoices and Payment Entries this importer created (identified by
        the remarks marker)."""
        seen: set = set()
        prefix = f"{self._MARKER}:"
        for doctype, extra in (
            ("Sales Invoice", {"is_opening": "Yes"}),
            ("Purchase Invoice", {"is_opening": "Yes"}),
            ("Payment Entry", {}),
        ):
            filters = {"company": self.company, "docstatus": ["<", 2],
                       "remarks": ["like", f"{prefix}%"]}
            filters.update(extra)
            for remark in frappe.get_all(
                    doctype, filters=filters, pluck="remarks") or []:
                if remark and remark.startswith(prefix):
                    seen.add(remark.strip())
        return seen


# ── Opening stock importer ───────────────────────────────────────────────────

@contextlib.contextmanager
def _accounting_dimensions_cached():
    """Memoise ERPNext's ``get_accounting_dimensions()`` for the duration of the block.

    Posting opening stock submits one Stock Reconciliation per chunk, and ERPNext's
    GL/SLE path calls ``get_accounting_dimensions()`` about twice per item - each an
    uncached DB read of a company-wide constant (the Accounting Dimension list does
    not change during a migration). On a real book that is ~2 round-trips per item of
    pure waste, and it dominates on Frappe Cloud where every round-trip carries
    network latency. We memoise the result for the phase, then restore the original.

    The function is name-imported into several ERPNext modules (general_ledger,
    accounts_controller, ...), so patching only its source module would miss those
    bound references. We scan ``sys.modules`` and swap every module-level reference
    that currently points at the original, restoring each on exit. The wrapper returns
    a fresh copy per call so a caller that mutates the list can never corrupt the
    cache. No-op-safe: if ERPNext or the symbol is absent, the block runs unchanged."""
    try:
        from erpnext.accounts.doctype.accounting_dimension import accounting_dimension as _ad
    except Exception:
        yield
        return
    original = getattr(_ad, "get_accounting_dimensions", None)
    if original is None:
        yield
        return

    cache: dict = {}

    def cached(as_list=True):
        if as_list not in cache:
            cache[as_list] = original(as_list=as_list)
        value = cache[as_list]
        return list(value) if isinstance(value, list) else value

    # Every module that did ``from ...accounting_dimension import get_accounting_dimensions``
    # holds its own reference; patch them all (plus the source module).
    patched = [m for m in list(sys.modules.values())
               if getattr(m, "get_accounting_dimensions", None) is original]
    for m in patched:
        m.get_accounting_dimensions = cached
    try:
        yield
    finally:
        for m in patched:
            m.get_accounting_dimensions = original


class StockOpeningImporter:
    """Posts item opening stock as one submitted 'Opening Stock' Stock Reconciliation.

    Tally stores opening stock on the Stock Item master (``OpeningBalance`` = qty,
    ``OpeningRate`` = valuation). The masters export carries no godown-wise split,
    so all opening stock lands in a single default warehouse. The difference posts
    against 'Temporary Opening - <ABBR>', consistent with the opening-balance JE.
    """

    doctype = "Stock Reconciliation"

    def __init__(self, company: str, abbr: str):
        self.company = company
        self.abbr = abbr

    # ERPNext queues a Stock Reconciliation submit in the background once it has
    # more than 100 rows (stock_reconciliation.py); that background hop both breaks
    # synchronous posting here and is the kind of job that stalls on cloud. Posting
    # in <=100-row chunks keeps every submit synchronous and deterministic, bounds a
    # failure's blast radius, and bounds the per-document memory/transaction size.
    _CHUNK = 100

    def run(self, items: list, posting_date: str) -> ImportResult:
        result = ImportResult(self.doctype)
        warehouse = self._default_warehouse()
        if not warehouse:
            result.add_error("Opening Stock", "no warehouse found to hold opening stock")
            return result

        # Names reused across items would collide on ERPNext's global batch id, so
        # scope exactly those per item - the same rule the Batch importer applied,
        # computed from the same export so the batch_id we tag here matches the Batch
        # that was actually created.
        self._shared_batch_names = shared_batch_names(items)

        # Aggregate by (item_code, warehouse): a masters export can list the same
        # Stock Item more than once (Tally names are unique, so duplicate tags are
        # the same item) and two rows for the same item+warehouse would trip
        # ERPNext's "Same item and warehouse combination should be unique" and fail
        # the whole document. Keep one row per item+warehouse; on a genuine quantity
        # conflict keep the larger and warn. The warehouse key (not item alone) lets
        # an item legitimately carry opening stock in several godowns.
        by_key: dict[tuple, dict] = {}
        for it in items:
            for wh, raw_qty, raw_rate, raw_value, batch in self._placements(it, warehouse, result):
                self._add_opening_row(
                    by_key, it["_name"], wh, raw_qty, raw_rate, raw_value,
                    it.get("StandardCost"), result, batch=batch)
        rows = list(by_key.values())
        if not rows:
            return result  # no opening stock to post

        # A Stock Reconciliation rejects a non-stock item and that single bad row
        # fails the WHOLE document - losing every item's opening stock. Tally carries
        # opening quantities on service/non-stock ledgers too, so drop just those
        # rows (with a warning) and let the genuine stock still post.
        rows = self._drop_non_stock_rows(rows, items, result)
        if not rows:
            return result  # only non-stock items carried an opening quantity

        # Per-item idempotency: skip rows whose (item, warehouse) was already posted
        # by a prior run. Unlike a single aggregate document, chunked posting can be
        # safely resumed - an interrupted run re-runs and completes the missing rows
        # instead of either doubling stock or abandoning what never posted.
        posted = self._posted_keys()
        pending = [r for r in rows if (r["item_code"], r["warehouse"]) not in posted]
        if not pending:
            result.skipped += 1
            result.add_warning(
                "Opening Stock",
                "opening stock already posted for every item with an opening "
                "quantity - skipped to avoid double-counting.")
            return result

        # Batch-tracked rows post through a Serial and Batch Bundle, which ERPNext
        # refuses to build unless Stock Settings > "Activate Serial and Batch No for
        # Item" is on (it is off by default). It is a hard prerequisite for batch
        # stock in ERPNext v15+, so enable it once when this import actually carries
        # batch rows, and record it on the log so the change is visible.
        if any(r.get("use_serial_batch_fields") for r in pending):
            self._ensure_serial_batch_enabled(result)

        # Memoise the per-item Accounting Dimension lookup ERPNext repeats on every
        # GL/SLE posting (a company-wide constant) - the dominant avoidable round-trip
        # cost of this phase, especially on Frappe Cloud. See _accounting_dimensions_cached.
        with _accounting_dimensions_cached():
            for i in range(0, len(pending), self._CHUNK):
                chunk = pending[i:i + self._CHUNK]
                if not self._post_doc(chunk, posting_date, result):
                    # The chunk failed as a unit - retry each row on its own so one bad
                    # row (e.g. an unforeseen validation) cannot cost the other ~100
                    # their opening stock. A 1-row doc also submits synchronously.
                    for row in chunk:
                        self._post_doc([row], posting_date, result, isolate=True)
        return result

    def _post_doc(self, rows: list, posting_date: str, result: ImportResult,
                  isolate: bool = False) -> bool:
        """Insert + submit one Opening Stock reconciliation for ``rows``. Returns True
        on success. On failure rolls back; when ``isolate`` (the row-by-row retry of a
        failed chunk) the single bad row is recorded as an error so it is visible."""
        try:
            doc = frappe.get_doc({
                "doctype": "Stock Reconciliation",
                "purpose": "Opening Stock",
                "company": self.company,
                "posting_date": posting_date,
                "posting_time": "00:00:00",
                "expense_account": company_scoped("Temporary Opening", self.abbr),
                "items": rows,
            })
            doc.insert(ignore_permissions=True)
            doc.submit()
            frappe.db.commit()
            result.add_created(doc.name)
            return True
        except Exception as exc:
            frappe.db.rollback()
            if isolate:
                result.add_error(f"Opening Stock ({rows[0]['item_code']})", exc)
            return False

    def _posted_keys(self) -> set:
        """(item_code, warehouse) pairs already carried by a submitted Opening Stock
        reconciliation for this company - so a re-run skips them."""
        rows = frappe.db.sql(
            """select sri.item_code, sri.warehouse
               from `tabStock Reconciliation Item` sri
               inner join `tabStock Reconciliation` sr on sr.name = sri.parent
               where sr.company = %s and sr.purpose = 'Opening Stock'
                 and sr.docstatus = 1""",
            self.company)
        return {(code, wh) for code, wh in rows}

    @staticmethod
    def _drop_non_stock_rows(rows: list, items: list, result: ImportResult) -> list:
        """Remove opening rows for items ERPNext does not stock-track. The non-stock
        decision uses the very rule the Item importer applied (``TypeOfSupply`` =
        Services -> non-stock), so it matches the ``is_stock_item`` ERPNext actually
        stored - no DB round-trip - and a service ledger that carries an opening
        quantity is dropped with a warning instead of failing the whole document."""
        nonstock = {
            safe_item_code(it["_name"]): it["_name"]
            for it in items if not ItemImporter._is_stock_item(it)
        }
        if not nonstock:
            return rows
        kept = []
        for r in rows:
            name = nonstock.get(r["item_code"])
            if name is None:
                kept.append(r)
            else:
                result.add_warning(
                    name, "opening stock not posted - this is a service / non-stock "
                    "item in ERPNext, which cannot hold opening stock. Record its "
                    "opening value as a ledger balance if it should carry one.")
        return kept

    def _placements(self, it: dict, default_wh: str,
                    result: "ImportResult | None" = None) -> list[tuple]:
        """Where this item's opening stock lands → list of
        ``(warehouse, raw_qty, raw_rate, raw_value, batch)`` buckets fed to
        :meth:`_add_opening_row`.

        The governing invariant: the rows for an item must total ``item_qty x
        item_rate`` - the value the reconciliation counts - whatever the godown data
        looks like. So:

        - No godown-wise detail (``GodownOpenings`` empty, e.g. a live client or an
          older export) → one bucket at the default warehouse using the item-level
          OpeningBalance/Rate/Value, i.e. exactly the legacy behaviour.
        - Godown-wise detail present → net each allocation into its (resolved
          warehouse, batch) bucket using SIGNED quantity. Tally's per-godown rows can
          include negatives (transfers / oversold) that are only authoritative once
          summed back to the item total. A clean split (every netted bucket >= 0 and
          the buckets reproduce the item-level quantity) posts one row per bucket;
          each is valued at the *item-level* rate, distributing only the quantity, so
          the posted total ties exactly to the item value. Per-godown rate/value is
          deliberately not trusted - it is frequently blank (then the row would post
          at zero value) or internally inconsistent with the item total.
        - When the godown split is not faithfully postable (a net-negative bucket,
          which ERPNext opening stock cannot hold, or buckets that do not tie to the
          item-level quantity) → a NON-batch item falls back to the single item-level
          row, which always reconciles (per-warehouse detail is sacrificed for that one
          item, not its value). A BATCH-tracked item cannot fall back this way: every
          opening row needs a ``batch_no`` and the item-level row has none, so ERPNext
          rejects it. Such an item's batch allocations genuinely cannot be represented
          as ERPNext opening stock (e.g. a batch quantity that nets negative), so it is
          skipped with a warning for manual entry - never posted at a wrong value.
        ``batch`` is "" for non-batch items, so they behave exactly as before.
        """
        item_qty = TallyExtractor.item_opening_qty(it)
        # Tally stamps EVERY item's opening allocation with a batch name - an implicit
        # "Primary Batch" even for non-batch items - so the batch is only meaningful
        # when the item is actually batch-tracked. Otherwise batch_no would be set on a
        # has_batch_no=0 item and the reconciliation would be rejected.
        is_batch = (it.get("IsBatchWiseOn") or "").strip().lower() == "yes"
        legacy = [(default_wh, it.get("OpeningBalance"),
                   it.get("OpeningRate"), it.get("OpeningValue"), "")]
        godowns = it.get("GodownOpenings") or []
        if not godowns:
            # A batch item needs a batch on every row; with no godown/batch detail
            # there is no batch to post under, so skip rather than emit a batch-less
            # row ERPNext rejects. A non-positive qty would be dropped anyway.
            if is_batch and item_qty > 0:
                return self._skip_unpostable_batch(it, result)
            return legacy
        # A non-positive item quantity is not postable (and the legacy row drops it
        # with a warning); never let a stray positive godown row post against it.
        if item_qty <= 0:
            return legacy
        buckets: dict[tuple, float] = {}
        for g in godowns:
            wh = self._warehouse_for_godown(g.get("godown", ""), default_wh)
            batch = (g.get("batch") or "").strip() if is_batch else ""
            buckets[(wh, batch)] = buckets.get((wh, batch), 0.0) + \
                TallyExtractor._parse_quantity(g.get("qty"))
        # Faithfulness gate: a net-negative bucket cannot be posted, and a breakdown
        # that does not sum to the item-level quantity would mis-state it.
        unfaithful = (any(qty < -1e-9 for qty in buckets.values())
                      or abs(sum(buckets.values()) - item_qty) > 1e-6)
        if unfaithful:
            # A non-batch item falls back to the item-level row (still reconciles). A
            # batch item cannot - the fallback row has no batch_no - and its batch
            # allocations cannot be represented as ERPNext opening stock, so skip it.
            if is_batch:
                return self._skip_unpostable_batch(it, result)
            return legacy
        # Value every bucket at the item-level rate, distributing only the quantity.
        item_rate = TallyExtractor.item_opening_rate(it)
        placements: list[tuple] = []
        for (wh, batch), qty in buckets.items():
            if qty <= 1e-9:
                continue
            placements.append((wh, str(qty), str(item_rate), "", batch))
        return placements or legacy

    @staticmethod
    def _skip_unpostable_batch(it: dict, result: "ImportResult | None") -> list:
        """Skip a batch-tracked item whose Tally batch allocations cannot be posted as
        ERPNext opening stock - a batch quantity that nets negative, a godown/batch
        split that does not reconcile to the item total, or no batch detail at all.
        ERPNext opening stock cannot hold these, and a batch item cannot fall back to a
        batch-less item-level row, so skip with a warning (manual entry) rather than
        post a wrong value or fail the whole document. Returns no placements."""
        if result is not None:
            result.add_warning(
                it.get("_name"),
                "opening stock not posted - this batch-tracked item's Tally batch "
                "allocations cannot be represented as ERPNext opening stock (a batch "
                "quantity is negative, or the per-batch split does not reconcile to "
                "the item's total). Set this item's opening stock manually.")
        return []

    def _warehouse_for_godown(self, godown: str, default_wh: str) -> str:
        """Map a Tally godown name to its migrated ERPNext warehouse
        (``"<Godown> - <ABBR>"``), falling back to the default warehouse when that
        godown was not migrated (e.g. Tally's implicit "Main Location")."""
        g = (godown or "").strip()
        if g:
            cand = company_scoped(g, self.abbr)
            if frappe.db.exists(
                    "Warehouse", {"name": cand, "is_group": 0, "company": self.company}):
                return cand
        return default_wh

    def _add_opening_row(self, by_key: dict, name: str, warehouse: str, raw_qty,
                         raw_rate, raw_value, raw_std_cost, result: ImportResult,
                         batch: str = "") -> None:
        """Build and dedupe one opening-stock row for ``name`` at ``warehouse``.

        Carries the full per-row treatment (negative/unreadable-quantity drops, the
        rate cascade, the value/rate cross-check, the zero-rate allowance, and the
        item+warehouse de-duplication), so both the legacy single-warehouse path and
        the per-godown path share identical behaviour. ``batch`` (when set) tags the
        row with its Batch and widens the de-dup key, so a batch-tracked item can hold
        opening stock in several batches at the same warehouse."""
        qty = TallyExtractor._parse_quantity(raw_qty)
        if qty < 0:
            # Tally can carry a negative opening quantity (e.g. oversold stock);
            # an 'Opening Stock' reconciliation cannot hold a negative qty and
            # would fail the whole document. Drop this line with a warning rather
            # than silently or fatally.
            result.add_warning(
                name,
                f"opening stock not posted - Tally reports a negative opening "
                f"quantity '{raw_qty}'. ERPNext opening stock cannot be negative; "
                "review the item in Tally and set its opening stock manually.")
            return
        if qty == 0:
            # A non-empty cell that parses to zero is a real opening quantity we
            # failed to read (e.g. an unexpected format) - surface it instead of
            # dropping the item's opening stock silently.
            if str(raw_qty or "").strip():
                result.add_warning(
                    name,
                    f"opening stock not posted - could not read quantity "
                    f"'{raw_qty}'. Set this item's opening stock manually.")
            return
        # Valuation: prefer the opening rate (unit-suffixed, e.g. "1.00/Nos"), then
        # the value Tally already computed (OpeningValue ÷ qty - sign is direction,
        # so abs), then the item's standard cost. _to_float can't read the unit
        # suffix, hence _parse_rate.
        opening_rate = TallyExtractor._parse_rate(raw_rate)
        opening_value = abs(BaseImporter._to_float(raw_value))
        rate = opening_rate
        if rate == 0 and opening_value:
            rate = opening_value / qty
        if rate == 0:
            rate = TallyExtractor._parse_rate(raw_std_cost)
        # Item Master Rule 1: when Tally gives BOTH an opening rate and value they
        # must satisfy value = qty x rate (Tally derives one from the other). A
        # divergence beyond rounding means the source is internally inconsistent;
        # we post using the opening rate, but flag that the posted value will not
        # match Tally's recorded value rather than letting it diverge silently.
        if opening_rate and opening_value:
            expected = qty * opening_rate
            if abs(opening_value - expected) > max(1.0, 0.01 * expected):
                result.add_warning(
                    name,
                    f"opening stock value does not reconcile - Tally reports a "
                    f"value of {opening_value:,.2f}, but quantity x rate is "
                    f"{qty:g} x {opening_rate:g} = {expected:,.2f}. Posted using "
                    "the opening rate; verify this item's opening stock in Tally.")

        code = safe_item_code(name)
        key = (code, warehouse, batch)
        prev = by_key.get(key)
        if prev is not None:
            if abs(prev["qty"] - qty) > 1e-9:
                result.add_warning(
                    name,
                    f"item appears more than once in the export with different "
                    f"opening quantities ({prev['qty']:g} vs {qty:g}); kept the "
                    f"larger. Verify the item's opening stock in Tally.")
                if qty <= prev["qty"]:
                    return
            else:
                return  # exact duplicate tag - same item exported twice
        row = {
            "item_code": code,
            "warehouse": warehouse,
            "qty": qty,
            "valuation_rate": rate,
        }
        if batch:
            # Batch-tracked item: tag the opening row with its Batch so the
            # reconciliation posts the quantity into that batch (the Batch master is
            # created first by BatchImporter).
            #
            # ERPNext v15+ models batch/serial stock through a Serial and Batch
            # Bundle, not the legacy batch_no field. Stock Reconciliation.validate
            # (set_current_serial_and_batch_bundle) throws "Please add Serial and
            # Batch Bundle for Item ..." for any batch row unless use_serial_batch_fields
            # is set. Setting it keeps the plain batch_no path: on_submit's
            # make_bundle_using_old_serial_batch_fields then builds the Bundle from
            # batch_no automatically. Without the flag the whole aggregate
            # reconciliation fails to submit and no opening stock posts at all.
            #
            # Use the same per-item-scoped batch id the Batch importer created: a
            # name shared across items (a rate, or Tally's implicit "Primary Batch")
            # is global in ERPNext, so without scoping the row would reference a
            # Batch that belongs to a different item ("Batch X does not belong to
            # Item Y") and fail.
            row["batch_no"] = batch_id_for(
                batch, code, getattr(self, "_shared_batch_names", set()))
            row["use_serial_batch_fields"] = 1
        if rate == 0:
            # ERPNext rejects a positive opening qty at a zero rate
            # ("Valuation Rate required for Item …") unless the row explicitly
            # allows it. Tally itself carries no value for these items, so we
            # post the quantity faithfully at zero value rather than blocking
            # the whole reconciliation. One warning per item, with identical
            # text - the log groups same-reason rows into a single line.
            row["allow_zero_valuation_rate"] = 1
            result.add_warning(
                name,
                "opening stock posted with a zero valuation rate - Tally carries "
                "no opening rate, value or standard cost for this item, so its "
                "opening stock has no book value. Set a valuation rate in ERPNext "
                "if it should carry value.")
        by_key[key] = row

    @staticmethod
    def _ensure_serial_batch_enabled(result: ImportResult) -> None:
        """Turn on Stock Settings > 'Activate Serial and Batch No for Item' if off.

        Idempotent and global (Stock Settings is a Single). Required for ERPNext to
        create the Serial and Batch Bundle that batch-tracked opening stock posts
        through; we set it only when this import actually has batch rows.

        The change is committed immediately: the chunk-posting loop that follows rolls
        back a failed chunk (``_post_doc``), and an uncommitted setting would be
        reverted by that rollback - leaving every later batch chunk to fail with
        "Activate Serial and Batch No for Item" and dropping all batch opening stock.
        Committing once, up front, makes the flag survive any later chunk rollback."""
        if frappe.db.get_single_value("Stock Settings", "enable_serial_and_batch_no_for_item"):
            return
        frappe.db.set_single_value("Stock Settings", "enable_serial_and_batch_no_for_item", 1)
        frappe.db.commit()
        result.add_warning(
            "Opening Stock",
            "enabled Stock Settings > 'Activate Serial and Batch No for Item' - it "
            "was off, and ERPNext requires it to post batch-tracked opening stock. "
            "Leave it on for batch/serial items to keep working.")

    def _default_warehouse(self) -> str:
        """A non-group warehouse to hold opening stock.

        Prefer Stock Settings' default, then the migrated default warehouse, then
        any leaf warehouse for the company.
        """
        ss = frappe.db.get_single_value("Stock Settings", "default_warehouse")
        # Stock Settings' default is global, so on a multi-company site it can point
        # at another company's warehouse - scope it to ours or the Stock Reconciliation
        # is rejected ("Warehouse X does not belong to company Y").
        if ss and frappe.db.exists(
                "Warehouse", {"name": ss, "is_group": 0, "company": self.company}):
            return ss
        candidate = company_scoped(DEFAULT_WAREHOUSE, self.abbr)
        if frappe.db.exists("Warehouse", {"name": candidate, "is_group": 0}):
            return candidate
        rows = frappe.get_all(
            "Warehouse", filters={"company": self.company, "is_group": 0},
            pluck="name", limit=1,
        )
        return rows[0] if rows else ""
