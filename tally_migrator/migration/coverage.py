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
          "clean": bool,                       # nothing unmapped
          "unmapped_field_count": int,         # total distinct unmapped tags
          "types": [
            {"entity_type": "Ledger",
             "unmapped": [{"field","count","sample","examples":[names]}]}
          ]
        }
    """
    types = []
    total = 0
    for obj_type, fields in MAPPED_FIELDS.items():
        known = {_norm(f) for f in fields} | {"NAME"}
        tags = source.raw_tags(obj_type)
        unmapped = [
            {
                "field": tag,
                "count": info.get("count", 0),
                "sample": info.get("sample", ""),
                "examples": info.get("records", []),
            }
            for tag, info in sorted(tags.items())
            if tag not in known and tag not in IGNORED_TAGS
        ]
        if unmapped:
            total += len(unmapped)
            types.append({"entity_type": obj_type, "unmapped": unmapped})
    return {
        "clean": total == 0,
        "unmapped_field_count": total,
        "types": types,
    }
