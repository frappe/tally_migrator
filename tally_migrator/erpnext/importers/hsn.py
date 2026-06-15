"""India Compliance HSN-validation toggle, suspended during item import."""

import contextlib
from collections import Counter
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
from tally_migrator.naming import safe_item_code, company_scoped
from tally_migrator.tally.extractors import TallyExtractor
from tally_migrator.validation.engine import (
    infer_gst_category, validate_gstin, GSTIN_STATE_CODES,
)

# ── India Compliance HSN validation toggle ───────────────────────────────────
# India Compliance can be set (GST Settings -> "Validate HSN Code") to reject any
# Item without a valid HSN/SAC code at save time. A Tally export often carries no
# item-level HSN, so that setting would fail the bulk of the item import. We switch
# it off for the duration of the item import and restore it after, so the items
# land (HSN left blank and flagged) without permanently relaxing the site's
# compliance posture. A cache marker outlives a hard-killed worker, so a run that
# dies mid-import is healed by _restore_hsn_validation() at the next run's start.
_HSN_GUARD_KEY = "tally_migrator:hsn_validate_disabled"


def _hsn_validation_field_present() -> bool:
    """True only when India Compliance's GST Settings + the ``validate_hsn_code``
    field both exist, so a site without India Compliance is a clean no-op."""
    try:
        if not frappe.db.exists("DocType", "GST Settings"):
            return False
        return bool(frappe.get_meta("GST Settings").get_field("validate_hsn_code"))
    except Exception:
        return False


def _set_hsn_validation(value: int) -> None:
    frappe.db.set_single_value("GST Settings", "validate_hsn_code", value)
    frappe.db.commit()


def _hsn_marker_key() -> str:
    site = getattr(frappe.local, "site", "") or ""
    return f"{_HSN_GUARD_KEY}:{site}"


def _restore_hsn_validation() -> None:
    """Run-start guard: if a previous run disabled HSN validation and a hard kill
    stopped the restore, turn it back on. Fail-safe = validation ON. Keyed on a
    cache marker that outlives a killed worker, so the setting self-heals."""
    try:
        cache = frappe.cache()
        if cache.get_value(_hsn_marker_key()):
            if _hsn_validation_field_present():
                _set_hsn_validation(1)
            cache.delete_value(_hsn_marker_key())
    except Exception:
        pass


@contextlib.contextmanager
def _hsn_validation_suspended():
    """Turn off India Compliance HSN validation for the item import, then restore
    it. Yields True when it actually suspended, False on a no-op. No-op when India
    Compliance is absent or the setting is already off - we never re-enable
    something the user deliberately turned off. The cache marker lets
    _restore_hsn_validation() recover the setting if this process is killed before
    the finally runs."""
    if not _hsn_validation_field_present() or not frappe.db.get_single_value(
            "GST Settings", "validate_hsn_code"):
        yield False
        return
    try:
        frappe.cache().set_value(_hsn_marker_key(), "1")
    except Exception:
        pass
    _set_hsn_validation(0)
    try:
        yield True
    finally:
        try:
            _set_hsn_validation(1)
            frappe.cache().delete_value(_hsn_marker_key())
        except Exception:
            # Leave the marker so the next run's _restore_hsn_validation re-enables it.
            pass
