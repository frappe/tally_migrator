"""Resolve the Units of Measure used in a Tally file against ERPNext.

Pure logic, no Frappe - the endpoint supplies the set of existing ERPNext UOM
names; this class diffs the Tally units against them. Kept out of the API layer
so the whitelisted endpoint stays thin (mirrors validation/engine.py).
"""
from __future__ import annotations

import re
from typing import Iterable

from tally_migrator.tally.mappings import UOM_MAP

# Tally gives a service item the placeholder base unit "Not Applicable" (often
# prefixed with a stray control char, e.g. "\x04 Not Applicable"). It is not a
# real UOM - the service Item just keeps ERPNext's mandatory default (Nos) - so
# it must never surface as a unit to create. Strip control chars before matching.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f]")


def _clean_uom(raw: str) -> str:
    return _CONTROL_CHARS.sub("", raw or "").strip()


def _is_real_uom(raw: str) -> bool:
    cleaned = _clean_uom(raw)
    return bool(cleaned) and cleaned.lower() != "not applicable"


class UomResolver:
    """Diff Tally units against the UOMs that already exist in ERPNext."""

    def __init__(self, existing_uoms: Iterable[str]):
        self._existing = {(u or "").strip() for u in existing_uoms if (u or "").strip()}

    def issues_for(self, tally_uoms: Iterable[str]) -> list[dict]:
        """One row per unique non-blank Tally unit, mapped + flagged exists/missing."""
        issues = []
        for tally_uom in self._unique(tally_uoms):
            mapped = UOM_MAP.get(tally_uom, tally_uom)  # fallback: Tally name as-is
            issues.append({
                "tally_uom": tally_uom,
                "erpnext_uom": mapped,
                "exists": mapped in self._existing,
            })
        return issues

    @property
    def existing_sorted(self) -> list[str]:
        """Existing ERPNext UOM names, for the 'map to existing' dropdown."""
        return sorted(self._existing)

    @staticmethod
    def _unique(values: Iterable[str]) -> list[str]:
        return sorted({_clean_uom(v) for v in values if _is_real_uom(v)})
