"""Undo a migration by deleting exactly the records it created.

This is a self-contained, optional feature. It reads the authoritative
``created_records`` manifest a run already stores on its ``Tally Migration Log``
(every ERPNext document the run actually inserted, with its real doctype) and
deletes precisely those documents - nothing scoped by company, so a company's
manually-entered data and any *other* migration's data are never touched.

Isolation (so the feature can be hidden or removed cleanly)
----------------------------------------------------------
* Hidden at runtime by the ``tally_migrator_enable_rollback`` site-config flag -
  :func:`is_enabled` gates both this endpoint and the form button, so even a
  crafted API call cannot reach the deletion logic when the flag is off.
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

import frappe
from frappe import _
from frappe.utils import get_link_to_form

from tally_migrator.api import ALLOWED_ROLES

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


def is_enabled() -> bool:
    """True only when the site explicitly opts in via site_config.

    Defaults to off: rollback permanently deletes posted documents, so it ships
    dormant and a site owner turns it on deliberately
    (``bench set-config tally_migrator_enable_rollback 1``).
    """
    return bool(frappe.conf.get("tally_migrator_enable_rollback"))


@frappe.whitelist()
def rollback_enabled() -> bool:
    """Client-facing read of the feature flag, so the form button renders only
    when rollback is switched on for this site."""
    return is_enabled()


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
            doc.cancel()
            for ledger in _LEDGER_DOCTYPES:
                frappe.db.delete(ledger, {"voucher_no": name})
        # force=False is deliberate: it keeps frappe's link-existence check, which is
        # the ONLY thing protecting a master (Item/Customer/Supplier/Cost Center -
        # whose on_trash does NOT guard against linked transactions) from being deleted
        # while a document created AFTER the migration still references it. force=True
        # would silently delete it and leave that later document dangling - the exact
        # corruption the except-branch below claims to avoid. On a clean undo nothing
        # links to the record (the run's own invoices/JE/stock recon are deleted
        # first), so the unforced delete still succeeds; only genuinely re-linked
        # records raise LinkExistsError, are caught, and are kept + reported.
        frappe.delete_doc(doctype, name, ignore_permissions=True)
        return None
    except Exception as exc:
        frappe.db.rollback(save_point=savepoint)
        # Almost always a link from activity created *after* the migration (a later
        # invoice/payment, stock still in a warehouse). Keep the record and tell the
        # user why, rather than forcing the delete and corrupting their books.
        return str(exc) or exc.__class__.__name__
    finally:
        _drop_queued_messages(msg_mark)


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
    if not is_enabled():
        frappe.throw(_("Migration rollback is not enabled on this site."),
                     frappe.PermissionError)

    log = frappe.get_doc("Tally Migration Log", log_name)

    if (company_confirmation or "").strip() != (log.company or "").strip():
        frappe.throw(_("The company name you typed does not match this migration's "
                       "company. Nothing was deleted."))
    if log.status == "Reverted":
        frappe.throw(_("This migration has already been reverted."))

    # One revert at a time per log - block a double-click, pointing at the run
    # already in flight (the same guard ERPNext's TDR uses).
    in_flight = frappe.get_all(
        "Tally Migration Revert",
        filters={"migration_log": log.name, "status": ("in", ["Queued", "In Progress"])},
        pluck="name")
    if in_flight:
        frappe.throw(_("An undo is already in progress for this migration: {0}.").format(
            get_link_to_form("Tally Migration Revert", in_flight[0])))

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
        timeout=3600,
        revert_name=revert.name,
    )
    return {"revert": revert.name}


def run_revert(revert_name: str) -> None:
    """Background worker: delete the manifest, fill the result table, set status.

    Runs the deletion the queued ``revert`` describes: deletes what it safely can,
    records every document (deleted or kept-with-reason) as a child row, updates
    the counts and status, and flips the source log to ``Reverted``. Failures mark
    the revert ``Failed`` with a traceback rather than leaving it stuck ``Queued``.
    """
    revert = frappe.get_doc("Tally Migration Revert", revert_name)
    try:
        revert.db_set("status", "In Progress")
        log = frappe.get_doc("Tally Migration Log", revert.migration_log)
        rows = _deletion_order(frappe.parse_json(log.created_records or "{}"))
        protected = _protected_doctypes()

        deleted = kept = 0
        for row in rows:
            reason = _delete_one(row, protected)
            revert.append("records", {
                "deleted": 0 if reason else 1,
                "reference_doctype": row["doctype"],
                "reference_name": row["name"],
                "entity": row["label"],
                "reason": reason or "",
            })
            if reason:
                kept += 1
            else:
                deleted += 1

        revert.deleted_count = deleted
        revert.kept_count = kept
        revert.status = "Completed with Errors" if kept else "Completed"
        revert.save(ignore_permissions=True)

        # The one honest edit to the log: its status must tell the truth everywhere
        # (list view, form, filters). The import path never sets this value.
        log.db_set("status", "Reverted")
        frappe.db.commit()
    except Exception:
        frappe.db.rollback()
        revert.reload()
        revert.db_set("status", "Failed")
        revert.db_set("error_log", frappe.get_traceback())
        frappe.db.commit()
        raise
    finally:
        frappe.publish_realtime(
            "tally_revert_updated", {"revert": revert_name},
            user=frappe.session.user)
