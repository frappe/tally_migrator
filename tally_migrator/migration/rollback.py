"""Undo a migration by deleting exactly the records it created.

This is a self-contained, optional feature. It reads the authoritative
``created_records`` manifest a run already stores on its ``Tally Migration Log``
(every ERPNext document the run actually inserted, with its real doctype) and
deletes precisely those documents - nothing scoped by company, so a company's
manually-entered data and any *other* migration's data are never touched.

Isolation (so the feature can be removed cleanly)
-------------------------------------------------
* The action is always available to a user with a migration role, but it is
  still safe by construction: it only deletes the documents in a run's
  ``created_records`` manifest, re-checks the company name server-side, and keeps
  anything since re-linked by later activity.
* All server logic lives in this one module; the heavy per-revert detail lives in
  the bolt-on ``Tally Migration Revert`` doctype. The import pipeline
  (``master_migrator.py`` / ``api.py``) is never touched.

Why not reuse ERPNext's Transaction Deletion Record? Its unit of work is the
*company* (it deletes every transaction with a matching company field and resets
company defaults), which both overshoots - wiping data this migration never
created - and undershoots - it ignores the global masters (Item, Customer,
Supplier, UOM, Item Group) a migration creates. We reuse its safety helpers
(:func:`_protected_doctypes`) but not its company-wide engine.
"""

import contextlib

import frappe
from frappe import _
from frappe.utils import get_link_to_form, now_datetime, time_diff_in_seconds

from tally_migrator.api import ALLOWED_ROLES
from tally_migrator.migration import profiler


def _stream_revert_progress(revert_name, pct, desc, deleted, total, prof):
    """Diagnostics: stream revert progress + the compact profile to a crash-proof
    cache the monitor can poll (mirrors MasterMigrator's progress cache). Best-effort:
    a cache hiccup must never abort an otherwise-good revert. Never merged to main."""
    try:
        frappe.cache().set_value(
            f"tally_revert_progress:{revert_name}",
            {"percent": pct, "description": desc, "deleted": deleted, "total": total,
             "profile": prof.compact() if prof else {}},
            expires_in_sec=6 * 60 * 60,
        )
    except Exception:
        pass


# The background job's own timeout (see ``enqueue`` below). A revert still flagged
# Queued/In Progress for longer than this, plus a margin, cannot still be running -
# the worker was killed, timed out, or OOM'd before its ``except`` could mark it
# Failed - so a fresh revert is allowed to supersede it (see ``revert_migration``).
_JOB_TIMEOUT = 3600
_STALE_AFTER = _JOB_TIMEOUT + 600

# Commit completed deletions in batches rather than holding the whole revert in one
# transaction (ERPNext's Transaction Deletion Record does the same): a large
# migration would otherwise hold row locks for the entire run and risk lock-wait
# timeouts, and a crash near the end would roll back everything. Each record is
# atomic via its own savepoint, so committing between records is safe; a crash mid-run
# leaves a prefix deleted, and a re-run is idempotent (already-gone records are skipped).
_COMMIT_BATCH = 50

# Maps a legacy log's bare-string entries (older runs stored just a name per
# pipeline label) to a doctype. New runs store ``{name, doctype}`` and don't need
# this. Mirrors CREATED_DOCTYPE in tally_migration_log.js - keep the two in step.
_LABEL_DOCTYPE = {
    "Accounts": "Account",
    "Cost Centres": "Cost Center",
    "Warehouses": "Warehouse",
    "Units": "UOM",
    "Stock Groups": "Item Group",
    "Customers": "Customer",
    "Suppliers": "Supplier",
    "Items": "Item",
    # "Prices" is intentionally omitted: it creates TWO doctypes (Item Price +
    # Pricing Rule), so a single label->doctype fallback can't represent it. New runs
    # store {name, doctype} per entry, so they need no fallback; legacy bare-string
    # runs predate the Prices/BOMs steps, so none exist to resolve.
    "BOMs": "BOM",
    "Opening Balances": "Journal Entry",
    "Opening Stock": "Stock Reconciliation",
}


def _protected_doctypes() -> set:
    """ERPNext's own 'never delete these' set, used as a safety backstop.

    Best-effort: if the helper isn't available (ERPNext layout drift), fall back
    to an empty set rather than failing the whole revert - the manifest only ever
    holds doctypes this app created, so this is a second line of defence."""
    try:
        from erpnext.setup.doctype.transaction_deletion_record.transaction_deletion_record import (
            get_protected_doctypes,
        )
        return set(get_protected_doctypes() or [])
    except Exception:
        return set()


# Creation order of the pipeline's entity labels (mirrors MasterMigrator._pipeline).
# Dependents come after their dependencies here (Items after Warehouses/Groups;
# Party Openings - the invoices/payments that reference a party's address - after
# Customers/Suppliers). Deletion walks this in REVERSE so dependents are removed
# first. We must NOT rely on the manifest's own key order: it is stored via
# frappe.as_json, which sorts keys alphabetically, so the stored order is not the
# creation order. A label not listed here is treated as most-dependent (deleted
# first), which is the safe default.
_CREATION_ORDER = [
    "Accounts", "Cost Centres", "Warehouses", "Units", "Stock Groups",
    "Customers", "Suppliers", "Items",
    # Prices (Item Price + Pricing Rule) and BOMs are created after Items (they
    # reference the item) and before the opening entries - so reversed deletion
    # removes them before the Items they lean on. Pipeline idx 82/84.
    "Prices", "BOMs",
    "Opening Balances", "Party Openings", "Opening Stock",
]


def _deletion_order(created: dict) -> list[dict]:
    """Flatten the manifest into a single delete-me list in dependency-safe order.

    Order is imposed by :data:`_CREATION_ORDER` (reversed), never by the manifest's
    own key order - that is alphabetised by ``as_json`` and would, for example, put
    Supplier before Purchase Invoice and leave the supplier undeletable (its address
    is still referenced by the invoice). Within a label the list order is preserved
    from creation and reversed, so a child created late (a Bank Account after its
    Account) is removed before what it leaned on.

    Each returned row is ``{name, doctype, label}``; entries that can't be resolved
    to a doctype are skipped (and reported by the caller as kept).
    """
    rows_by_label: dict[str, list[dict]] = {}
    for label, items in created.items():
        rows: list[dict] = []
        for item in items or []:
            if isinstance(item, str):
                name, doctype = item, _LABEL_DOCTYPE.get(label, "")
            else:
                name = item.get("name")
                doctype = item.get("doctype") or _LABEL_DOCTYPE.get(label, "")
            if name and doctype:
                rows.append({"name": name, "doctype": doctype, "label": label})
        rows.reverse()  # within a label, delete late-created rows first
        rows_by_label[label] = rows

    def creation_rank(label: str) -> int:
        # Unknown labels rank highest (a future pipeline entity is most likely a
        # late-created dependent), so reverse order deletes them first.
        return _CREATION_ORDER.index(label) if label in _CREATION_ORDER else len(_CREATION_ORDER)

    ordered: list[dict] = []
    for label in sorted(rows_by_label, key=creation_rank, reverse=True):
        ordered.extend(rows_by_label[label])
    return ordered


# Ledger entries a submitted voucher owns. ERPNext does NOT remove these on
# cancel (it marks them cancelled but keeps the rows), and they then link back to
# the voucher and block its deletion. We purge the ones this voucher owns - the
# same ledgers ERPNext's own Transaction Deletion Record clears - so the voucher
# can be deleted. They are scoped by voucher_no, so only this voucher's entries go.
_LEDGER_DOCTYPES = ("GL Entry", "Payment Ledger Entry", "Stock Ledger Entry")

# Derived records ERPNext auto-generates around a master - recompute queues and
# unit-conversion rows - that link the master but are NOT business data and never
# enter our manifest. They block an unforced delete (e.g. a Repost Item Valuation
# queued when the opening Stock Reconciliation is cancelled keeps every item alive;
# a UOM Conversion Factor for an imported compound unit keeps its UOMs alive). We
# clear only the rows tied to the master being removed - the same records ERPNext's
# Transaction Deletion Record purges - so deletion is not blocked by our own
# import's side effects. Maps master doctype -> (side-effect doctype, field) pairs.
_SIDE_EFFECTS = {
    "Item": (("Repost Item Valuation", "item_code"),),
    "UOM": (("UOM Conversion Factor", "from_uom"),
            ("UOM Conversion Factor", "to_uom"),
            # India Compliance's GST Settings child rows the UQC step writes; absent
            # on a site without india_compliance, so the purge is table-guarded.
            ("GST UOM Map", "uom")),
    # The auto-created "Tally Imported" UOM Category is blocked by the UOM Conversion
    # Factors that name it. Purge those when the category is deleted so it goes
    # regardless of whether every owning UOM was removed first (order-independent) -
    # they are this run's own conversions (all in this category), so this is safe.
    "UOM Category": (("UOM Conversion Factor", "category"),),
}


def _drop_queued_messages(mark: int) -> None:
    """Discard framework messages queued since ``mark``.

    ERPNext's cancel/delete path emits msgprint notices (and the link-check throw
    queues its message before raising). Left in place they flush to the client as
    a wall of red popups. We catch every failure and report it in the kept-records
    list instead, so these queued messages are noise - drop them so the user sees
    only our single summary."""
    log = getattr(frappe.local, "message_log", None)
    if isinstance(log, list) and len(log) > mark:
        del log[mark:]


def _safe_savepoint_rollback(savepoint: str) -> None:
    """Roll back this record's savepoint; fall back to a full rollback if it's gone.

    The per-record safety guarantee rests on this savepoint. It is normally present,
    but a doctype's ``on_cancel`` handler *could* commit mid-record (some stock/repost
    paths do), which silently discards the savepoint and makes ``ROLLBACK TO SAVEPOINT``
    raise. Rather than let that abort the whole revert, fall back to rolling back the
    open transaction - safe because the loop batch-commits completed records, so this
    discards at most the current partial record and any others since the last commit,
    all of which a re-run will simply redo (deletion is idempotent)."""
    try:
        frappe.db.rollback(save_point=savepoint)
    except Exception:
        frappe.db.rollback()


def _purge_party_links(party_type: str, party: str) -> list[dict]:
    """Release the Address/Contact/Bank Account records that keep a party undeletable.

    A party's contact info is created alongside it (from Tally party data), is not
    in the manifest, and is linked by a Dynamic Link row (a child of the Address /
    Contact) that keeps the party undeletable - ERPNext does not cascade it.

    Critical safety rule: an Address/Contact can be linked to *several* parties, and
    because the link lives on the address side, ``delete_doc`` would NOT refuse to
    delete a shared one - it would succeed and silently strip the record from a
    party this migration never touched. So we only delete a record that links to no
    party other than this one; if it is shared, we drop just this party's own link
    row, leaving the record intact for the others. Either way the party can then be
    removed. Deleting an unshared record stays unforced, so if some *other* document
    (e.g. a later user invoice's ``customer_address``) still references it, the
    delete raises, propagates to the caller, and the party is kept and reported
    rather than the record being force-removed.

    Returns the business records it deleted (``[{doctype, name}]``) so the caller can
    list them in the revert report - these are real Tally data (a party's addresses,
    contacts, bank accounts), not silent plumbing, so the audit must show them.
    """
    purged: list[dict] = []
    # The party doc holds its own primary links (customer_primary_address /
    # _contact, or the supplier_* equivalents - all core ERPNext fields). These
    # point AT the Address/Contact, so delete_doc on that record is refused
    # ("Cannot delete ... linked with <Party>") and the party is kept. The party is
    # about to be deleted, so clear the links first; a direct db.set_value (plain
    # UPDATE, no doc reload) is enough and the field set differs only by prefix.
    prefix = party_type.lower()
    for field in (f"{prefix}_primary_address", f"{prefix}_primary_contact"):
        frappe.db.set_value(party_type, party, field, None)
    for linked_dt in ("Contact", "Address"):
        names = frappe.get_all(
            "Dynamic Link",
            filters={"link_doctype": party_type, "link_name": party,
                     "parenttype": linked_dt},
            pluck="parent")
        for nm in set(names):
            if not frappe.db.exists(linked_dt, nm):
                continue
            links = frappe.get_all(
                "Dynamic Link",
                filters={"parent": nm, "parenttype": linked_dt},
                fields=["link_doctype", "link_name"])
            shared = any((l.link_doctype, l.link_name) != (party_type, party)
                         for l in links)
            if shared:
                # Keep the record for the other party; remove only our own link so
                # this party stops being referenced and can be deleted.
                frappe.db.delete("Dynamic Link",
                                 {"parent": nm, "parenttype": linked_dt,
                                  "link_doctype": party_type, "link_name": party})
            else:
                frappe.delete_doc(linked_dt, nm, ignore_permissions=True,
                                  delete_permanently=True)
                purged.append({"doctype": linked_dt, "name": nm})
    # A Bank Account created for the party (from Tally bank details) links it via its
    # own party_type/party fields and blocks the delete too. It belongs to exactly one
    # party (not a shared child-link record), so there is no shared-record case here.
    for nm in frappe.get_all("Bank Account",
                             filters={"party_type": party_type, "party": party},
                             pluck="name"):
        if frappe.db.exists("Bank Account", nm):
            frappe.delete_doc("Bank Account", nm, ignore_permissions=True,
                              delete_permanently=True)
            purged.append({"doctype": "Bank Account", "name": nm})
    return purged


def _delete_one(row: dict, protected: set) -> str | None:
    """Delete one document; return a human reason if it had to be kept, else None.

    Submitted documents (opening JE, party invoices/payments, stock recon) are
    cancelled first, then the ledger entries they own are purged so the delete is
    not blocked by ERPNext's retained cancelled ledgers. Everything runs inside a
    savepoint so a failure here rolls back only this record - never the deletions
    already done - leaving the revert able to continue and report the kept record
    instead of aborting half-done.
    """
    doctype, name = row["doctype"], row["name"]

    if doctype in protected:
        return _("{0} is a protected doctype and was not deleted").format(doctype)
    if not frappe.db.exists(doctype, name):
        return None  # already gone (e.g. cascaded with a parent) - nothing to do

    msg_mark = len(getattr(frappe.local, "message_log", []) or [])
    savepoint = "tm_revert"
    frappe.db.savepoint(savepoint)
    try:
        doc = frappe.get_doc(doctype, name)
        if getattr(doc, "docstatus", 0) == 1:
            # Clear any stale document lock before cancelling. A large doc whose cancel
            # was previously routed to the background Submission Queue (see below) leaves a
            # lock file that its never-run async job never releases; on a re-run that lock
            # would raise "currently locked and queued" and wedge this record again. We own
            # the teardown (single active revert) and are about to delete this doc, so
            # clearing its lock is safe. Best-effort: no lock -> harmless no-op.
            try:
                doc.unlock()
            except Exception:
                pass
            # Cancel synchronously. Some ERPNext doctypes route cancel() to a background
            # Submission Queue when the document is large (e.g. a Journal Entry with more
            # than 100 account rows - an opening-balance entry always is). In a background
            # revert that path can't complete: it enqueues an async cancel and returns with
            # the doc still submitted (so the delete below fails and the record is wrongly
            # kept) or contends on the doc lock. Call the internal _cancel() - the exact
            # action the queue would have run (frappe's queue_action maps "cancel" ->
            # "_cancel"), still firing before_cancel/on_cancel via save - so cancellation
            # always happens inline. Fall back to cancel() for anything without _cancel.
            cancel_inline = getattr(doc, "_cancel", None) or doc.cancel
            cancel_inline()
            for ledger in _LEDGER_DOCTYPES:
                frappe.db.delete(ledger, {"voucher_no": name})
            # The opening Stock Reconciliation auto-creates a Serial and Batch Bundle
            # per batch-tracked row (we set use_serial_batch_fields on those rows).
            # Cancelling the reconciliation only delinks the Bundle and flags it
            # is_cancelled - the submitted Bundle doc lingers with voucher_no still
            # pointing here, and ERPNext's link check would then block the unforced
            # delete of the reconciliation, leaving it kept. Cancel already removed
            # its stock effect (the Stock Ledger Entries were purged just above), and
            # is_cancelled lets the Bundle's on_trash guard pass, so force-delete each
            # Bundle (cascading its child entries) before the voucher is removed.
            if frappe.db.table_exists("Serial and Batch Bundle"):
                for bundle in frappe.get_all(
                        "Serial and Batch Bundle",
                        filters={"voucher_no": name}, pluck="name"):
                    frappe.delete_doc("Serial and Batch Bundle", bundle,
                                      force=True, ignore_permissions=True,
                                      delete_permanently=True)
        # Clear ERPNext's derived side-effect rows for this master (Repost Item
        # Valuation, UOM Conversion Factor, GST UOM Map) before the delete, so they
        # don't block it. Scoped to this master's name, inside the savepoint, so a
        # failure here rolls back with the rest of this record's attempt. Guarded by
        # table_exists so a doctype an optional app owns (GST UOM Map) is simply
        # skipped when that app is not installed.
        for se_doctype, field in _SIDE_EFFECTS.get(doctype, ()):
            if frappe.db.table_exists(se_doctype):
                frappe.db.delete(se_doctype, {field: name})
        # A party's Address/Contact (created from Tally party data) is dynamically
        # linked and blocks its delete; ERPNext never cascades them. Remove them so
        # the party can go - unforced, so one shared with another party stays and the
        # party is reported kept rather than corrupting the shared record.
        if doctype in ("Customer", "Supplier"):
            row["_purged"] = _purge_party_links(doctype, name)
        # force=False is deliberate: it keeps frappe's link-existence check, which is
        # the ONLY thing protecting a master (Item/Customer/Supplier/Cost Center -
        # whose on_trash does NOT guard against linked transactions) from being deleted
        # while a document created AFTER the migration still references it. force=True
        # would silently delete it and leave that later document dangling - the exact
        # corruption the except-branch below claims to avoid. On a clean undo nothing
        # links to the record (the run's own invoices/JE/stock recon are deleted
        # first), so the unforced delete still succeeds; only genuinely re-linked
        # records raise LinkExistsError, are caught, and are kept + reported.
        frappe.delete_doc(doctype, name, ignore_permissions=True,
                          delete_permanently=True)
        return None
    except Exception as exc:
        _safe_savepoint_rollback(savepoint)
        # Almost always a link from activity created *after* the migration (a later
        # invoice/payment, stock still in a warehouse). Keep the record and tell the
        # user why, rather than forcing the delete and corrupting their books.
        return str(exc) or exc.__class__.__name__
    finally:
        _drop_queued_messages(msg_mark)


@contextlib.contextmanager
def _quiet_framework():
    """Silence the per-record framework bookkeeping that dominates a large revert.

    A revert of tens of thousands of documents spends almost all of its wall-clock
    time not on the deletes themselves but on the side work each delete/cancel
    triggers:

    * a realtime ``list_update`` / ``doc_update`` event per document (and per Error
      Log / Comment the cancel path writes); any desk client watching the revert then
      re-runs its notification open-count aggregation - measured on a real run as the
      bulk of the runtime (~14k aggregations over Error Log and other doctypes);
    * an activity ``Comment`` feed row per delete (``insert_feed``);
    * one enqueued ``delete_dynamic_links`` job per document, which also floods the
      background queue (the earlier "Queue Overloaded" failure).

    None of it is needed for an undo - we keep our own audit in the revert's
    ``records`` table, and the site is not serving those list views mid-revert. We set
    ``frappe.flags.in_import`` (which ``notify_update`` and ``insert_feed`` already
    honour as "bulk data churn, do not emit") and run the dynamic-link cleanup inline
    instead of enqueuing a job per record. Both are saved and restored, so normal
    behaviour resumes the instant the revert finishes or raises. The flag only gates
    validate/insert/naming and notification paths - never the cancel/delete logic this
    revert relies on - so correctness is unchanged; it purely quietens the framework.
    """
    prev_in_import = frappe.flags.in_import
    real_enqueue = frappe.enqueue

    def _inline_enqueue(method=None, **kwargs):
        name = method if isinstance(method, str) else getattr(method, "__name__", "")
        if name.endswith("delete_dynamic_links"):
            # Keep the cleanup, drop the per-record job: run it now, synchronously.
            for k in ("queue", "timeout", "now", "enqueue_after_commit",
                      "job_name", "at_front", "job_id"):
                kwargs.pop(k, None)
            return frappe.call(method, **kwargs)
        return real_enqueue(method, **kwargs)

    frappe.flags.in_import = True
    frappe.enqueue = _inline_enqueue
    try:
        yield
    finally:
        frappe.flags.in_import = prev_in_import
        frappe.enqueue = real_enqueue


def _record_row(parent: str, idx: int, *, deleted: int, doctype: str,
                name: str, entity: str, reason: str) -> None:
    """Persist one revert audit row immediately, rather than buffering to the end.

    Inserts a ``Tally Migration Revert Item`` child directly so a killed worker keeps
    the audit for everything it had already deleted. The old path appended every row
    to the parent in memory and wrote them all with a single ``revert.save()`` at the
    very end: a job that hit the 60-minute timeout mid-run then persisted nothing and
    reported 0 deleted though thousands were already gone. A direct child insert is
    O(1) per row (``revert.save()`` would rewrite the whole child table each batch,
    O(n^2)), and committing per batch bounds what a crash loses."""
    frappe.get_doc({
        "doctype": "Tally Migration Revert Item",
        "parenttype": "Tally Migration Revert",
        "parentfield": "records",
        "parent": parent,
        "idx": idx,
        "deleted": deleted,
        "reference_doctype": doctype,
        "reference_name": name,
        "entity": entity,
        "reason": reason,
    }).db_insert()


@frappe.whitelist(methods=["POST"])
def revert_migration(log_name: str, company_confirmation: str):
    """Queue an undo of a migration, after confirming the company name.

    ``company_confirmation`` must equal the log's ERPNext company (the user types
    it to arm the action); it is re-checked here so the server never trusts the
    client. The deletion itself can touch hundreds of submitted documents, so it
    runs in a background job (mirroring ERPNext's Transaction Deletion Record): we
    create a ``Tally Migration Revert`` record in ``Queued`` state, enqueue the
    work, and return its name immediately so the UI can link the user to it to
    watch progress.
    """
    frappe.only_for(ALLOWED_ROLES)

    log = frappe.get_doc("Tally Migration Log", log_name)

    if (company_confirmation or "").strip() != (log.company or "").strip():
        frappe.throw(_("The company name you typed does not match this migration's "
                       "company. Nothing was deleted."))
    if log.status == "Reverted":
        frappe.throw(_("This migration has already been reverted."))

    # One revert at a time per log - block a double-click, pointing at the run
    # already in flight (the same guard ERPNext's TDR uses). But a revert flagged
    # Queued/In Progress past the job timeout cannot still be running (the worker was
    # killed/timed out before its except could mark it Failed); mark such a corpse
    # Failed and let this new revert proceed, so a crash never wedges the feature.
    in_flight = frappe.get_all(
        "Tally Migration Revert",
        filters={"migration_log": log.name, "status": ("in", ["Queued", "In Progress"])},
        fields=["name", "modified"])
    live = []
    for r in in_flight:
        if time_diff_in_seconds(now_datetime(), r.modified) > _STALE_AFTER:
            frappe.db.set_value("Tally Migration Revert", r.name, {
                "status": "Failed",
                "error_log": _("Marked Failed: the background job exceeded its timeout "
                               "without completing (worker killed, timed out, or OOM)."),
            })
        else:
            live.append(r.name)
    if live:
        frappe.throw(_("An undo is already in progress for this migration: {0}.").format(
            get_link_to_form("Tally Migration Revert", live[0])))

    created = frappe.parse_json(log.created_records or "{}")
    if not _deletion_order(created):
        frappe.throw(_("This migration has no recorded created documents to delete."))

    revert = frappe.new_doc("Tally Migration Revert")
    revert.migration_log = log.name
    revert.company = log.company
    revert.status = "Queued"
    revert.insert(ignore_permissions=True)
    frappe.db.commit()

    frappe.enqueue(
        "tally_migrator.migration.rollback.run_revert",
        queue="long",
        timeout=_JOB_TIMEOUT,
        revert_name=revert.name,
    )
    return {"revert": revert.name}


@frappe.whitelist()
def preview_revert(log_name: str) -> dict:
    """Read-only dry summary of what an undo *would* delete, for the confirm dialog.

    Reads the manifest and breaks the would-delete documents down by doctype, so the
    user sees ``12 Item, 5 Customer, 3 Journal Entry...`` before arming the action,
    not just a bare total. Deletes nothing and writes nothing - the heavy cancel/delete
    work only happens in the enqueued run after confirmation.
    """
    frappe.only_for(ALLOWED_ROLES)
    log = frappe.get_doc("Tally Migration Log", log_name)
    rows = _deletion_order(frappe.parse_json(log.created_records or "{}"))
    by_doctype: dict[str, int] = {}
    for r in rows:
        by_doctype[r["doctype"]] = by_doctype.get(r["doctype"], 0) + 1
    return {
        "total": len(rows),
        "company": log.company,
        "already_reverted": log.status == "Reverted",
        # Most-numerous doctype first, so the dialog leads with the bulk of the work.
        "by_doctype": dict(sorted(by_doctype.items(), key=lambda kv: -kv[1])),
    }


def run_revert(revert_name: str) -> None:
    """Background worker: delete the manifest, fill the result table, set status.

    Runs the deletion the queued ``revert`` describes: deletes what it safely can,
    records every document (deleted or kept-with-reason) as a child row, updates
    the counts and status, and flips the source log to ``Reverted``. Failures mark
    the revert ``Failed`` with a traceback rather than leaving it stuck ``Queued``.
    """
    revert = frappe.get_doc("Tally Migration Revert", revert_name)
    # Diagnostics: profile the revert (SQL fingerprints, per-record timing, commits,
    # enqueues, RSS) and stream progress + a compact profile to a crash-proof cache the
    # monitor polls. Best-effort and fully restored in `finally`. Never merged to main.
    prof = profiler.RunProfiler()
    _sess = profiler.session(prof)
    _sess.__enter__()
    _phase_cm = None
    try:
        revert.db_set("status", "In Progress")
        log = frappe.get_doc("Tally Migration Log", revert.migration_log)
        rows = _deletion_order(frappe.parse_json(log.created_records or "{}"))
        protected = _protected_doctypes()

        _phase_cm = prof.phase("Revert", len(rows))
        _phase_cm.__enter__()
        _stream_revert_progress(revert_name, 0, "Starting revert...", 0, len(rows), prof)

        # Split off records already absent before we start (manifest entries this run
        # never actually created, or ones removed earlier - including everything a
        # prior, timed-out revert already deleted). Snapshot now, before the loop, so a
        # record that genuinely cascades with a parent we delete this run is NOT
        # mistaken for one that was already gone. Absent records are audited up front as
        # "already absent" and kept out of the delete loop so they are not recounted as
        # deletions; this is also what makes a re-run of a killed revert resumable.
        pending, pre_absent = [], []
        for r in rows:
            (pre_absent if not frappe.db.exists(r["doctype"], r["name"]) else pending).append(r)
        total_deletable = len(pending)

        idx = 0
        for r in pre_absent:
            idx += 1
            _record_row(revert.name, idx, deleted=0, doctype=r["doctype"],
                        name=r["name"], entity=r["label"],
                        reason=_("Already absent before this undo - not created by this "
                                 "run, or removed earlier."))
        frappe.db.commit()

        # Delete in dependency order, retrying whatever was kept. A record can be
        # blocked on one pass by a sibling removed later in the same run (a UOM still
        # held by an Item deleted afterwards; a party still linked by a voucher
        # processed later). Repeat until a pass deletes nothing new, so only records
        # genuinely re-linked by post-migration activity are left kept. Each deletion is
        # audited and its batch committed as we go (see _record_row / _COMMIT_BATCH), so
        # a killed worker leaves a truthful partial result and a re-run resumes from what
        # is left. The loop runs inside _quiet_framework() so the per-record realtime /
        # feed / backup / queue churn - not the deletes - stops dominating the runtime.
        deleted = kept = since_commit = 0
        with _quiet_framework():
            while pending:
                still, progressed = [], False
                for row in pending:
                    with profiler.record(f"{row['doctype']} {row['name']}", row):
                        reason = _delete_one(row, protected)
                    if reason:
                        row["_reason"] = reason
                        still.append(row)
                        continue
                    progressed = True
                    idx += 1
                    _record_row(revert.name, idx, deleted=1, doctype=row["doctype"],
                                name=row["name"], entity=row["label"], reason="")
                    deleted += 1
                    # Party-attached business data (addresses, contacts, bank accounts)
                    # removed alongside the party - audited so the trail shows them too.
                    for p in row.get("_purged", []):
                        idx += 1
                        _record_row(revert.name, idx, deleted=1, doctype=p["doctype"],
                                    name=p["name"],
                                    entity=_("{0} (linked to {1})").format(
                                        row["label"], row["name"]),
                                    reason="")
                        deleted += 1
                    since_commit += 1
                    if since_commit >= _COMMIT_BATCH:
                        revert.db_set("deleted_count", deleted,
                                      update_modified=False, commit=True)
                        since_commit = 0
                        _pct = int(90 * deleted / total_deletable) if total_deletable else 90
                        _stream_revert_progress(
                            revert_name, _pct,
                            f"Deleting {deleted} of {total_deletable}...",
                            deleted, len(rows), prof)
                pending = still
                if not progressed:
                    break
        frappe.db.commit()  # flush the final, partial batch of deletions

        # Whatever is still pending after the loop is genuinely kept (re-linked by
        # later activity, or a protected doctype); audit each with its reason.
        for row in pending:
            idx += 1
            _record_row(revert.name, idx, deleted=0, doctype=row["doctype"],
                        name=row["name"], entity=row["label"],
                        reason=row.get("_reason", ""))
            kept += 1

        revert.db_set("deleted_count", deleted, update_modified=False)
        revert.db_set("kept_count", kept, update_modified=False)
        revert.db_set("status", "Completed with Errors" if kept else "Completed")

        # The one honest edit to the log: its status must tell the truth everywhere
        # (list view, form, filters). The import path never sets this value. A clean
        # undo is the terminal "Reverted" (the action is then withdrawn); one that kept
        # records becomes "Reverted with Errors", which deliberately stays re-runnable
        # so the user can retry after fixing the cause (a re-run is idempotent).
        log.db_set("status", "Reverted with Errors" if kept else "Reverted")
        frappe.db.commit()
        if _phase_cm is not None:
            _phase_cm.__exit__(None, None, None)
            _phase_cm = None
        _stream_revert_progress(
            revert_name, 100,
            f"Revert complete - deleted {deleted}, kept {kept}.",
            deleted, deleted + kept, prof)
    except Exception:
        frappe.db.rollback()
        revert.reload()
        revert.db_set("status", "Failed")
        revert.db_set("error_log", frappe.get_traceback())
        frappe.db.commit()
        raise
    finally:
        # Diagnostics: close the profiler phase/session (restores the SQL/enqueue hooks)
        # even on failure, so a crashed revert still leaves its captured profile in cache.
        try:
            if _phase_cm is not None:
                _phase_cm.__exit__(None, None, None)
        except Exception:
            pass
        try:
            _sess.__exit__(None, None, None)
        except Exception:
            pass
        # Notify the user who launched the undo (the revert's owner) - in a background
        # job frappe.session.user is the worker's, not theirs, so it would not reach
        # the form they are watching.
        frappe.publish_realtime(
            "tally_revert_updated", {"revert": revert_name},
            user=frappe.db.get_value("Tally Migration Revert", revert_name, "owner"))
