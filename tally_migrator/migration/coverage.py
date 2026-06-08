"""Field-coverage report: what's in the Tally file that we DON'T migrate.

The extractor reads a fixed allow-list of fields per object type (see the
``*_FIELDS`` lists in ``tally.extractors``). Anything outside that list — Tally
UDFs, extra ledger/item attributes, custom columns — is never read, so it can
never reach ERPNext. That's a silent loss.

This module compares the tags actually present in the uploaded file against the
mapped allow-list and reports the difference, so the user sees exactly which
fields will not be migrated *before* running, and the same report is stored on
the migration log for audit.

Pure logic (no Frappe): it asks the source only for ``raw_tags(obj_type)``, so
it is fully unit-testable with a stub source.
"""
from __future__ import annotations

from tally_migrator.tally.extractors import (
    LEDGER_FIELDS, ITEM_FIELDS, GODOWN_FIELDS, GROUP_FIELDS, COSTCENTRE_FIELDS,
)

# Tally object type → the fields the extractor fetches (what CAN enter the pipeline).
MAPPED_FIELDS: dict[str, list] = {
    "Group": GROUP_FIELDS,
    "Ledger": LEDGER_FIELDS,
    "Stock Item": ITEM_FIELDS,
    "Godown": GODOWN_FIELDS,
    "Cost Centre": COSTCENTRE_FIELDS,
}

# The fields an importer actually PERSISTS onto an ERPNext doc. Fetching a field
# (MAPPED_FIELDS) is not the same as writing it: a field can be in the FETCH list
# yet never reach ERPNext. Anything mapped-but-not-written is a silent loss that
# the plain "is it in the allow-list" check would mask — so we track it explicitly.
# Keep this in lock-step with the importers (tally_migrator.erpnext.importers).
WRITTEN_FIELDS: dict[str, list] = {
    # Groups → Account name + parent (AccountImporter).
    "Group": ["Name", "Parent"],
    # Ledgers → Customer/Supplier (party) or Account. Across the type every listed
    # field lands somewhere: party tax/pan/contact/address/opening, or account
    # name/parent/opening.
    "Ledger": LEDGER_FIELDS,
    # Stock Item → every fetched field is written (ItemImporter + opening stock):
    # name/group/uom/rates/description/hsn, GST treatment, and TypeOfSupply →
    # is_stock_item, plus OpeningBalance/OpeningRate → opening Stock Reconciliation.
    "Stock Item": ITEM_FIELDS,
    # Godown → Warehouse name/parent/address (WarehouseImporter).
    "Godown": GODOWN_FIELDS,
    # Cost Centre → name + parent (CostCentreImporter).
    "Cost Centre": COSTCENTRE_FIELDS,
}

# Tally housekeeping/structural tags — present on most masters but not business
# data, so flagging them as "unmapped" would only be noise.
IGNORED_TAGS = {
    "NAME", "GUID", "MASTERID", "ALTERID", "ALTERID.LIST",
    "LANGUAGENAME.LIST", "NAME.LIST", "ISDELETED", "SORTPOSITION",
    "RESERVEDNAME", "FORPAYROLL", "ISGROUP",
}


def _norm(field: str) -> str:
    """Match the tag-derivation the source uses (uppercase, no spaces)."""
    return field.upper().replace(" ", "")


def coverage_report(source) -> dict:
    """Compare file tags against the mapped allow-list, per object type.

    ``source`` must expose ``raw_tags(obj_type)``. Returns a UI-/audit-ready dict::

        {
          "clean": bool,                       # nothing unmapped AND nothing unwritten
          "unmapped_field_count": int,         # tags we never read
          "unwritten_field_count": int,        # tags we read but never persist
          "types": [
            {"entity_type": "Ledger",
             "unmapped":  [{"field","count","sample","examples":[names]}],
             "unwritten": [{"field","count","sample","examples":[names]}]}
          ]
        }

    Two distinct losses are reported:
      • **unmapped**  — a tag in the file that the extractor never fetches (UDFs).
      • **unwritten** — a tag the extractor *does* fetch but no importer persists,
        so it's silently dropped despite looking "covered". This is the subtle
        gap the allow-list-only check used to miss.
    """
    types = []
    unmapped_total = 0
    unwritten_total = 0
    for obj_type, fields in MAPPED_FIELDS.items():
        mapped = {_norm(f) for f in fields} | {"NAME"}
        written = {_norm(f) for f in WRITTEN_FIELDS.get(obj_type, fields)} | {"NAME"}
        read_not_written = mapped - written
        tags = source.raw_tags(obj_type)

        unmapped, unwritten = [], []
        for tag, info in sorted(tags.items()):
            if tag in IGNORED_TAGS:
                continue
            row = {
                "field": tag,
                "count": info.get("count", 0),
                "sample": info.get("sample", ""),
                "examples": info.get("records", []),
            }
            if tag not in mapped:
                unmapped.append(row)
            elif tag in read_not_written:
                unwritten.append(row)

        if unmapped or unwritten:
            unmapped_total += len(unmapped)
            unwritten_total += len(unwritten)
            types.append({
                "entity_type": obj_type,
                "unmapped": unmapped,
                "unwritten": unwritten,
            })
    return {
        "clean": unmapped_total == 0 and unwritten_total == 0,
        "unmapped_field_count": unmapped_total,
        "unwritten_field_count": unwritten_total,
        "types": types,
    }
