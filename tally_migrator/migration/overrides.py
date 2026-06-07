"""Apply user-supplied per-record field overrides to extracted masters.

Pure logic, no Frappe. The overrides come from the pre-flight data-quality screen
where the user fixes flagged fields (GSTIN, state, HSN, …) before importing. They
are applied to the in-memory record dicts only — the uploaded XML is never touched,
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


def apply_record_overrides(masters, overrides: dict | None):
    """Patch records in ``masters`` in place. Returns ``masters`` for chaining.

    Blank override values are ignored (treated as "no change"), so an empty input
    on the screen never wipes existing data.
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
                if value not in (None, ""):
                    record[field] = value
    return masters
