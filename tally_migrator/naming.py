"""Shared ERPNext name transforms.

Single authority so that validation (collision detection) and import compute the
*same* item_code - otherwise the ITEM_CODE_COLLISION check would lie about what
the importer is actually going to create.
"""

import hashlib
from collections import defaultdict


def safe_item_code(name: str) -> str:
    """ERPNext item_code caps at 140 chars and dislikes '/'."""
    code = (name or "")[:140].replace("/", "-").strip()
    # Frappe reserves the literal string "New {Doctype}" for the unsaved-document
    # placeholder and refuses to save a real record under it - validate_name raises
    # NameError for any name starting with "New Item" (frappe/model/naming.py). Items
    # are named by item_code, so a Tally item literally called "New Item" is unsavable
    # as-is. Disambiguate just enough to clear the reserved prefix. This is the single
    # Tally-name -> item_code mapping every consumer shares (the Item itself AND every
    # batch / BOM / price / opening-stock reference to it), so the links stay
    # consistent and a re-run still matches. The original label is kept as item_name.
    # Matches frappe's own case-sensitive check, so only names it would reject change.
    if code.startswith("New Item"):
        code = f"Item - {code}"[:140]
    return code


def company_scoped(base: str, abbr: str) -> str:
    """ERPNext names company-scoped doctypes (Account, Warehouse, Cost Center) as
    ``<base> - <ABBR>``. Returns just ``<base>`` when no abbreviation is known.
    """
    base = (base or "").strip()
    abbr = (abbr or "").strip()
    return f"{base} - {abbr}" if abbr else base


def shared_batch_names(items: list) -> set:
    """Tally batch names used by more than one batch-tracked item in this export.

    ERPNext batch ids are global, but Tally batch names are per-item - so the same
    name on two items (a rate like '169.49', or Tally's implicit 'Primary Batch')
    would collide on a single global Batch. Identifying the shared names lets the
    Batch importer and the opening-stock importer scope exactly those ids per item
    and, computing from the same export, agree on the result.
    """
    by_name = defaultdict(set)
    for it in items or []:
        if (it.get("IsBatchWiseOn") or "").strip().lower() != "yes":
            continue
        code = safe_item_code(it.get("_name", ""))
        for g in (it.get("GodownOpenings") or []):
            batch = (g.get("batch") or "").strip()
            if batch:
                by_name[batch].add(code)
    return {batch for batch, codes in by_name.items() if len(codes) > 1}


def batch_id_for(tally_batch: str, item_code: str, shared: set) -> str:
    """The ERPNext ``batch_id`` to use for a Tally batch on a given item.

    A name shared across items is scoped to the item (``"<batch> - <item_code>"``)
    so each item gets its own Batch; a unique name is kept verbatim. Capped at 140
    chars (``Batch.batch_id`` is Data), preserving uniqueness with a short hash when
    the scoped id would overflow.
    """
    batch = (tally_batch or "").strip()
    if not batch or batch not in (shared or set()):
        return batch
    scoped = f"{batch} - {item_code}"
    if len(scoped) <= 140:
        return scoped
    digest = hashlib.md5(scoped.encode("utf-8")).hexdigest()[:8]
    return scoped[:131] + "-" + digest
