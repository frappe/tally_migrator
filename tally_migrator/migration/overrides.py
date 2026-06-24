"""Apply user-supplied per-record field overrides to extracted masters.

Pure logic, no Frappe. The overrides come from the pre-flight data-quality screen
where the user fixes flagged fields (GSTIN, state, HSN, …) before importing. They
are applied to the in-memory record dicts only - the uploaded XML is never touched,
so the source file stays an untouched audit artifact.

Override shape (JSON-friendly, nested by entity type then record name)::

    {
      "Customer": {"Delhi Modern Hardware": {"GSTRegistrationNumber": "07ABC..."}},
      "Item":     {"River Sand Grade A":   {"HSNCode": "2505"}}
    }
"""
from __future__ import annotations

# Maps the entity_type used in the UI/validation to the masters attribute.
_BUCKETS = (
    ("Customer", "customers"),
    ("Supplier", "suppliers"),
    ("Item", "items"),
)


def apply_record_overrides(masters, overrides: dict | None, changelog: list | None = None):
    """Patch records in ``masters`` in place. Returns ``masters`` for chaining.

    Blank override values are ignored (treated as "no change"), so an empty input
    on the screen never wipes existing data.

    If ``changelog`` is provided, every *effective* change (where the new value
    actually differs from the extracted value) is appended as a dict
    ``{entity_type, record_name, field, old, new}`` - an audit trail of exactly
    what the user edited on the pre-flight screen before importing.
    """
    if not overrides:
        return masters
    for entity_type, attr in _BUCKETS:
        by_name = overrides.get(entity_type) or {}
        if not by_name:
            continue
        for record in getattr(masters, attr, []):
            patch = by_name.get(record.get("_name"))
            if not patch:
                continue
            for field, value in patch.items():
                if value in (None, ""):
                    continue
                old = record.get(field, "")
                if str(old) == str(value):
                    continue  # no effective change - don't log a no-op
                if changelog is not None:
                    changelog.append({
                        "entity_type": entity_type,
                        "record_name": record.get("_name"),
                        "field": field,
                        "old": old,
                        "new": value,
                    })
                record[field] = value
    return masters


def uom_edits(uom_overrides: dict | None,
              created_uoms: list | None = None) -> list[dict]:
    """Audit-trail rows for the pre-flight UOM resolutions, in the same shape as the
    field-edit changelog so they can be folded into the same "Applied edits" table.

    A unit resolved on the Check screen is either *mapped to an existing* ERPNext UOM
    (e.g. Carton -> Box) or *created as a new* unit; ``created_uoms`` (the units the
    user chose to create) decides which label a row gets. ``old`` is the Tally unit
    and ``new`` the resulting ERPNext unit, so a create-with-rename (e.g. Pkt ->
    Packet) reads correctly too.

    Pure: automatic ``UOM_MAP`` normalisations never reach ``uom_overrides`` (only
    user-resolved units do), so they are naturally excluded - this lists user
    decisions only, exactly like the record overrides above.
    """
    created = set(created_uoms or [])
    return [
        {
            "entity_type": "Unit",
            "record_name": tally_uom,
            "field": ("Created as new unit" if erpnext_uom in created
                      else "Mapped to existing unit"),
            "old": tally_uom,
            "new": erpnext_uom,
        }
        for tally_uom, erpnext_uom in (uom_overrides or {}).items()
    ]
