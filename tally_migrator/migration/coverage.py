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


def _is_noise(tag: str, info: dict) -> bool:
    """True for Tally housekeeping tags that are never business data: audit fields,
    legacy-tax scaffolding, no-ERPNext-target attributes, empty ``.LIST`` containers
    and pure Yes/No/Not-Applicable config toggles. Tags carrying a real value with a
    real destination (a bank number, a city, a rate) are NOT noise and still surface."""
    if tag in _AUDIT_TAGS or tag in _NO_TARGET_TAGS or tag.startswith(_LEGACY_TAX_PREFIXES):
        return True
    sample = (info.get("sample") or "").strip().lower()
    if tag.endswith(".LIST") and not sample:
        return True                          # empty structural container
    return sample in _NOISE_VALUE_SENTINELS  # boolean toggle / unused-feature sentinel


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
      • **unmapped**  - a tag in the file that the extractor never fetches (UDFs).
      • **unwritten** - a tag the extractor *does* fetch but no importer persists,
        so it's silently dropped despite looking "covered". This is the subtle
        gap the allow-list-only check used to miss.
    """
    types = []
    unmapped_total = 0
    unwritten_total = 0
    noise_total = 0
    for obj_type, fields in MAPPED_FIELDS.items():
        mapped = _read_tags(obj_type, fields) | {"NAME"}
        written = _read_tags(obj_type, WRITTEN_FIELDS.get(obj_type, fields)) | {"NAME"}
        read_not_written = mapped - written
        tags = source.raw_tags(obj_type)

        unmapped, unwritten = [], []
        for tag, info in sorted(tags.items()):
            if tag in IGNORED_TAGS:
                continue
            # Tally-internal noise (flags/empty containers/audit/legacy tax) is
            # hidden from the report but counted, so the user knows it was dropped
            # without drowning the fields that actually carry data.
            if tag not in mapped and _is_noise(tag, info):
                noise_total += 1
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
        # Tally-internal fields intentionally suppressed from the per-field tables.
        "noise_field_count": noise_total,
        "types": types,
    }
