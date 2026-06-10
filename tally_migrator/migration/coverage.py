"""Field-coverage report: what's in the Tally file that we DON'T migrate.

The extractor reads a fixed allow-list of fields per object type (see the
``*_FIELDS`` lists in ``tally.extractors``). Anything outside that list - Tally
UDFs, extra ledger/item attributes, custom columns - is never read, so it can
never reach ERPNext. That's a silent loss.

This module compares the tags actually present in the uploaded file against the
mapped allow-list and reports the difference, so the user sees exactly which
fields will not be migrated *before* running, and the same report is stored on
the migration log for audit.

Pure logic (no Frappe): it asks the source only for ``raw_tags(obj_type)``, so
it is fully unit-testable with a stub source.
"""
from __future__ import annotations

import re

from tally_migrator.tally.extractors import (
    LEDGER_FIELDS, ITEM_FIELDS, GODOWN_FIELDS, GROUP_FIELDS, COSTCENTRE_FIELDS,
    STOCKGROUP_FIELDS, UNIT_FIELDS,
    LEDGER_TAGS, ITEM_TAGS, GODOWN_TAGS,
)

# Tally object type → the fields the extractor fetches (what CAN enter the pipeline).
MAPPED_FIELDS: dict[str, list] = {
    "Group": GROUP_FIELDS,
    "Ledger": LEDGER_FIELDS,
    "Stock Item": ITEM_FIELDS,
    "Godown": GODOWN_FIELDS,
    "Cost Centre": COSTCENTRE_FIELDS,
    "Stock Group": STOCKGROUP_FIELDS,
    "Unit": UNIT_FIELDS,
}

# Per-type tag overrides the extractor uses for fields whose real Tally tag isn't
# FIELD.upper() (see extractors.*_TAGS). The report derives the *actual* tag each
# field reads from these, so a genuine export's <LEDSTATENAME>/<EMAIL>/
# <ADDRESS.LIST>/<STANDARDPRICELIST.LIST> is counted as covered.
_TAGS_BY_TYPE: dict[str, dict] = {
    "Ledger": LEDGER_TAGS,
    "Stock Item": ITEM_TAGS,
    "Godown": GODOWN_TAGS,
}


def _read_tags(obj_type: str, fields: list) -> set[str]:
    """The set of top-level tags the parser actually reads for these fields -
    exactly mirroring ``FileTallySource.get_collection``: a field's tag override
    when one exists, else ``FIELD.upper()``. Nested ``.LIST`` paths surface as
    their container tag (``raw_tags`` only sees each record's direct children)."""
    overrides = _TAGS_BY_TYPE.get(obj_type, {})
    out: set[str] = set()
    for f in fields:
        for cand in (overrides.get(f) or [_norm(f)]):
            path = cand["path"] if isinstance(cand, dict) else cand
            out.add(_norm(path.split("/")[0]))
    return out


def read_tags(obj_type: str) -> set[str]:
    """Public: every tag the extractor reads for an object type (for tests)."""
    return _read_tags(obj_type, MAPPED_FIELDS.get(obj_type, []))

# The fields an importer actually PERSISTS onto an ERPNext doc. Fetching a field
# (MAPPED_FIELDS) is not the same as writing it: a field can be in the FETCH list
# yet never reach ERPNext. Anything mapped-but-not-written is a silent loss that
# the plain "is it in the allow-list" check would mask - so we track it explicitly.
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
    # Stock Group → Item Group name + parent (StockGroupImporter).
    "Stock Group": STOCKGROUP_FIELDS,
    # Unit → UOM name/whole-number + compound conversion (UnitImporter); every
    # fetched field feeds the UOM or its conversion factor.
    "Unit": UNIT_FIELDS,
}

# Tally housekeeping/structural tags - present on most masters but not business
# data, so flagging them as "unmapped" would only be noise.
IGNORED_TAGS = {
    "NAME", "GUID", "MASTERID", "ALTERID", "ALTERID.LIST",
    "LANGUAGENAME.LIST", "NAME.LIST", "ISDELETED", "SORTPOSITION",
    "RESERVEDNAME", "FORPAYROLL", "ISGROUP",
}

# A real TallyPrime export carries hundreds of internal config flags, empty
# structural containers and audit fields on every master. None of it is business
# data, so listing it as "won't migrate" buries the handful of fields that
# genuinely matter. These are suppressed from the per-field report (but still
# counted, so the user knows how many were hidden).
_NOISE_VALUE_SENTINELS = {"yes", "no", "not applicable", "create"}
_AUDIT_TAGS = {
    "OBJECTUPDATEACTION", "ISUPDATINGTARGETID", "ISSECURITYONWHENENTERED",
    "UPDATEDDATETIME", "ASORIGINAL", "TYPEOFUPDATEACTIVITY", "OLDAUDITENTRYIDS.LIST",
}
# Pre-GST tax frameworks Tally still emits but ERPNext doesn't model on masters.
_LEGACY_TAX_PREFIXES = (
    "EXCISE", "VAT", "SERVICETAX", "STX", "TDS", "TCS", "SCHVI", "XBRL",
    "LBT", "FBT", "SALESTAX", "CVD",
)
# Tags that DO carry a value but have no ERPNext destination - Tally-internal
# pointers, data redundant with a field we already import, pre-GST excise
# scaffolding, or attributes that only exist at a level ERPNext doesn't model.
# Suppressed from the per-field tables (still counted in ``noise_field_count``)
# so the report shows only fields with a real, fixable target. NOTE: a tag here is
# only hidden where it's *unmapped* - e.g. VALUATIONMETHOD is a real mapped field
# on a Stock Item (→ Item.valuation_method) yet has no home at Stock-Group level,
# so it stays visible on items and is hidden only on groups.
_NO_TARGET_TAGS = {
    "GRPCREDITPARENT", "GRPDEBITPARENT",    # Tally internal Dr/Cr group nature
    "CURRENCYNAME",                          # = company base currency (redundant)
    "LEDGERCOUNTRYISDCODE",                  # phone ISD code (redundant with country)
    "PRIORSTATENAME",                        # historical GST state (only current kept)
    "DEFAULTTRANSFERMODE",                   # payment mode; no ERPNext party field
    "TAXTYPE", "RATEOFVAT",                  # legacy tax classification / VAT rate
    "DENOMINATOR", "OPENINGVALUE",           # derived from qty × rate (already imported)
    "BASEUNITS",                             # stock-group default unit; Item Group has no UOM
    "COSTINGMETHOD", "VALUATIONMETHOD",      # only unmapped at stock-GROUP level
    "JOBNAME", "TAXUNITNAME",                # excise job-work / tax unit (pre-GST)
    "ARE1SERIALMASTER", "ARE2SERIALMASTER", "ARE3SERIALMASTER",  # excise ARE forms
    "BANKINGCONFIGBANKID",                   # Tally-internal id for the bank-config row
                                             # (the bank itself is imported via BANKINGCONFIGBANK)
    "STARTINGFROM",                          # "effective from" date stamped on Tally config
                                             # .LIST rows; a config marker, not business data
}


# Generic, high-precision shape signals so new internal tags are caught without
# being hand-listed - deliberately conservative, because wrongly hiding a real
# field is a silent data loss (the very thing this report guards against). Each
# rule fires only when a tag is almost certainly Tally-internal; anything with a
# real textual value (a city, a category, an account number) is left visible.
_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4,}){2,}")  # Tally GUID-ish
_YYYYMMDD_RE = re.compile(r"^\d{8}$")
_EFFECTIVE_DATE_SUFFIXES = ("FROM", "UPTO", "TILL")


def _looks_internal(tag: str, sample: str) -> bool:
    """Heuristic 'this tag is Tally-internal' test. High precision by design: it
    only returns True for shapes that carry no business meaning, never for a tag
    that holds a real value with a possible ERPNext home."""
    s = (sample or "").strip()
    if not s:
        return True                          # empty in every record - nothing to migrate
    if _GUID_RE.match(s):
        return True                          # GUID / internal object id
    if tag.endswith("ID") and s in ("0", "00"):
        return True                          # zeroed internal pointer (e.g. ...CONFIGBANKID)
    if tag.endswith(_EFFECTIVE_DATE_SUFFIXES) and _YYYYMMDD_RE.match(s):
        return True                          # config 'effective from/upto' date marker
    return False


def _is_noise(tag: str, info: dict) -> bool:
    """True for Tally housekeeping tags that are never business data: audit fields,
    legacy-tax scaffolding, no-ERPNext-target attributes, empty ``.LIST`` containers,
    pure Yes/No/Not-Applicable config toggles, and (via ``_looks_internal``) generic
    internal shapes - GUIDs, zeroed id pointers, effective-date markers, empties.
    Tags carrying a real value with a real destination (a bank number, a city, a
    rate) are NOT noise and still surface."""
    if tag in _AUDIT_TAGS or tag in _NO_TARGET_TAGS or tag.startswith(_LEGACY_TAX_PREFIXES):
        return True
    sample = (info.get("sample") or "").strip().lower()
    if tag.endswith(".LIST") and not sample:
        return True                          # empty structural container
    if sample in _NOISE_VALUE_SENTINELS:     # boolean toggle / unused-feature sentinel
        return True
    return _looks_internal(tag, info.get("sample", ""))


def _norm(field: str) -> str:
    """Match the tag-derivation the source uses (uppercase, no spaces)."""
    return field.upper().replace(" ", "")


# ── Derivation layer: read a field's meaning from its own shape, never from a
# hand-maintained list of tag names. Open-ended Tally UDFs differ on every export,
# so a curated dictionary would be a maintenance treadmill; these derivations work
# on tags neither we nor the user have seen before. (See project principle.)

def humanize_tag(tag: str) -> str:
    """Best-effort readable label derived from the raw tag itself - no lookup.

    Strips namespace / ``.LIST`` / separators and splits camelCase and
    letter/digit boundaries, then Title-cases. Tally export tags are usually
    UPPERCASE with no word boundaries (``CUSTOMERCATEGORY``), which no rule can
    re-segment without a dictionary - so for those the label is simply a tidied,
    Title-cased token. The value-shape phrase (``value_kind``) carries the real
    "what is this" signal; the label is a secondary aid. Prefer Tally's own
    display name upstream when an export provides one."""
    s = tag.split("}")[-1].split("/")[-1]                 # drop namespace + path
    s = s.replace(".LIST", "").replace(".", " ")
    s = s.replace("_", " ").replace("-", " ")
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)            # camelCase boundary
    s = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", s)            # letter->digit boundary
    s = re.sub(r"(?<=\d)(?=[A-Za-z])", " ", s)            # digit->letter boundary
    s = re.sub(r"\s+", " ", s).strip()
    return s.title() if s else tag


# Generic value-shape detectors: classify a sample VALUE, not the tag name, so the
# signal generalises to any file. Ordered most- to least-specific; first match wins.
_GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][0-9A-Z]Z[0-9A-Z]$", re.I)
_PAN_RE   = re.compile(r"^[A-Z]{5}\d{4}[A-Z]$", re.I)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^\+?[\d][\d\s\-()]{6,18}$")
_DATE_RE  = re.compile(r"^(\d{8}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})$")
_AMOUNT_RE = re.compile(r"^-?[\d,]*\.?\d+(\s*(dr|cr))?$", re.I)


def value_kind(sample: str) -> str:
    """A human phrase for what a field's values LOOK like, derived from a sample.

    Returns e.g. "GST numbers", "email addresses", "dates"; "" for plain text or
    when there's no sample. Reads the data's shape, so it works on unknown fields.
    """
    s = (sample or "").strip()
    if not s:
        return ""
    if _GSTIN_RE.match(s):
        return "GST numbers"
    if _PAN_RE.match(s):
        return "PAN numbers"
    if _EMAIL_RE.match(s):
        return "email addresses"
    if _DATE_RE.match(s):
        return "dates"
    if _PHONE_RE.match(s) and sum(c.isdigit() for c in s) >= 7:
        return "phone numbers"
    if _AMOUNT_RE.match(s):
        return "numbers"
    return ""


def _read_leaf_tags(obj_type: str, fields: list) -> set[str]:
    """Leaf (last-segment) names of every path the extractor reads for these fields.

    Mirrors ``_read_tags`` but keeps the path's *leaf* instead of its container, so
    a flat top-level tag can be matched against a value we already read from a
    NESTED path. E.g. a flat ``<GSTIN>`` matches the leaf of the mapped
    ``LEDGSTREGDETAILS.LIST/GSTIN`` - structural proof it's a redundant duplicate
    of imported data, derived by comparison rather than a hard-coded "GSTIN" rule.
    """
    overrides = _TAGS_BY_TYPE.get(obj_type, {})
    out: set[str] = set()
    for f in fields:
        for cand in (overrides.get(f) or [_norm(f)]):
            path = cand["path"] if isinstance(cand, dict) else cand
            out.add(_norm(path.split("/")[-1]))
    return out


def coverage_report(source) -> dict:
    """Compare file tags against the mapped allow-list, per object type.

    ``source`` must expose ``raw_tags(obj_type)``. Returns a UI-/audit-ready dict::

        {
          "clean": bool,                       # no actual data loss
          "unmapped_field_count": int,         # tags we never read (real loss)
          "unwritten_field_count": int,        # read but never persisted (real loss)
          "redundant_field_count": int,        # duplicate of data already imported
          "noise_field_count": int,            # Tally-internal, hidden
          "types": [
            {"entity_type": "Ledger",
             "unmapped":  [row], "unwritten": [row], "redundant": [row]}
          ]
        }

    where each ``row`` is
    ``{field, label, kind, count, sample, examples:[names]}`` - ``label`` and
    ``kind`` are derived (from the tag string / the value's shape), never looked
    up, so the report reads plainly for tags we've never seen.

    Three field classes are distinguished:
      • **unmapped**   - a tag in the file the extractor never fetches (a UDF). Loss.
      • **unwritten**  - a tag the extractor fetches but no importer persists. Loss.
      • **redundant**  - a flat tag whose values we already import via a nested path
        (e.g. flat ``<GSTIN>`` vs nested ``LEDGSTREGDETAILS.LIST/GSTIN``). NOT a loss;
        detected by leaf-name comparison, not a hard-coded rule.
    """
    types = []
    unmapped_total = 0
    unwritten_total = 0
    noise_total = 0
    redundant_total = 0
    for obj_type, fields in MAPPED_FIELDS.items():
        mapped = _read_tags(obj_type, fields) | {"NAME"}
        written = _read_tags(obj_type, WRITTEN_FIELDS.get(obj_type, fields)) | {"NAME"}
        read_not_written = mapped - written
        read_leaves = _read_leaf_tags(obj_type, fields)
        tags = source.raw_tags(obj_type)

        unmapped, unwritten, redundant = [], [], []
        for tag, info in sorted(tags.items()):
            if tag in IGNORED_TAGS:
                continue
            # Tally-internal noise (flags/empty containers/audit/legacy tax) is
            # hidden from the report but counted, so the user knows it was dropped
            # without drowning the fields that actually carry data.
            if tag not in mapped and _is_noise(tag, info):
                noise_total += 1
                continue
            # Every reported field carries a derived label + value-shape phrase so
            # the UI can speak plainly without a hand-maintained tag dictionary.
            row = {
                "field": tag,
                "label": humanize_tag(tag),
                "kind": value_kind(info.get("sample", "")),
                "count": info.get("count", 0),
                "sample": info.get("sample", ""),
                "examples": info.get("records", []),
            }
            if tag not in mapped:
                # A flat tag whose name matches a NESTED path we already read is a
                # duplicate of imported data, not a loss - reassure, don't alarm.
                if tag in read_leaves:
                    redundant.append(row)
                else:
                    unmapped.append(row)
            elif tag in read_not_written:
                unwritten.append(row)

        if unmapped or unwritten or redundant:
            unmapped_total += len(unmapped)
            unwritten_total += len(unwritten)
            redundant_total += len(redundant)
            types.append({
                "entity_type": obj_type,
                "unmapped": unmapped,
                "unwritten": unwritten,
                "redundant": redundant,
            })
    return {
        # "clean" = no actual data loss. Redundant duplicates and hidden noise are
        # not losses, so they don't make a file un-clean.
        "clean": unmapped_total == 0 and unwritten_total == 0,
        "unmapped_field_count": unmapped_total,
        "unwritten_field_count": unwritten_total,
        # Fields already imported via another (nested) path; shown reassuringly.
        "redundant_field_count": redundant_total,
        # Tally-internal fields intentionally suppressed from the per-field tables.
        "noise_field_count": noise_total,
        "types": types,
    }
