# Spec — Pre-flight Validation Engine (Gap 2)

> Port Tally Bridge's offline data-quality engine into `tally_migrator` and surface
> it as a **pre-flight issues screen**, reusing the exact UX pattern already built
> for UOM resolution. Read-only: it extracts and inspects, but **writes nothing**.

Status: **spec / not yet built.** Source of truth for the logic: Tally Bridge
`tally_bridge/validation.py` (+ `tests/test_validation.py`).

---

## 1. Why

The migration pipe is a commodity; the moat is catching India-GST data problems
**before** they fail (often silently) in ERPNext. Today Tally Migrator's only
pre-flight check is UOMs. This adds 7 more rule types over the data it already
extracts, in the same "show what's wrong → let the user decide → then migrate"
flow the user explicitly asked for ("you should not create anything on your own").

This is the highest value-per-effort gap because:
- `validation.py` is **pure functions, no network, no Frappe** — it ports almost
  verbatim and stays fully unit-testable locally (the one place our build-only
  constraint doesn't block tests).
- The data shapes already match: Migrator's extractor emits dicts keyed by `_name`
  with the same Tally field names (`GSTRegistrationNumber`, `LedgerState`,
  `CountryName`, `PinCode`, `HSNCode`, `LedgerPhone`, `LedgerMobile`) that
  `validation.py` reads.
- The UI pattern (compact scrollable table, grouped rows, one Continue) is already
  proven by the UOM screen.

---

## 2. Scope

**In scope (Phase 1 — masters validation only):**

| Code | Severity | Check | Entity |
|---|---|---|---|
| `GSTIN_INVALID` | Error | 15-char structure regex + valid state code + **mod-36 checksum** | Customer/Supplier |
| `GST_STATE_MISSING` | Error | India party with no `LedgerState` | Customer/Supplier |
| `GSTIN_STATE_MISMATCH` | Warning | GSTIN state code ≠ ledger state (flips CGST/SGST vs IGST) | Customer/Supplier |
| `PIN_STATE_CONFLICT` | Warning | PIN postal-circle prefix disagrees with state (coarse, warn-only) | Customer/Supplier |
| `HSN_MISSING` | Warning | Item has no HSN/SAC code | Item |
| `ITEM_CODE_COLLISION` | Error | Two items slug to the same `item_code` | Item |
| `DUPLICATE_PARTY` | Warning | Likely duplicate parties (union-find: name / GSTIN / phone / fuzzy ≥0.80) | Customer/Supplier |

**Out of scope for now (deferred to later gaps):**
- `VOUCHER_UNBALANCED` / `VOUCHER_EMPTY` — needs the vouchers feature (Gap 3) first.
- Live GSTIN *registry* checks (is this GSTIN real/active) — needs external API.
- `infer_gst_category` *as a validator* — but see §6, we should adopt it as an
  importer enrichment when we touch the party importers.

---

## 3. Backend — files & changes

### 3.1 New file: `tally_migrator/validation/__init__.py`
Empty package marker.

### 3.2 New file: `tally_migrator/validation/engine.py`
Port of `tally_bridge/validation.py`. Changes from the Bridge original:

1. **Imports.** Replace
   ```python
   from .naming import item_code
   from .tally.mappings import TALLY_STATE_MAP
   ```
   with Migrator equivalents:
   - `item_code(name)` → reuse `ItemImporter._safe_name` logic. **Action:** extract
     that slugging into a module-level `safe_item_code(name)` function in
     `tally_migrator/erpnext/importers.py` (or a small `naming.py`) and import it
     here, so validation and import compute the **same** code. (This guarantees the
     `ITEM_CODE_COLLISION` check matches what the importer will actually create.)
   - `TALLY_STATE_MAP` is imported in Bridge but unused in `validation.py` — **drop
     it** (it's dead in the original).
2. **Keep verbatim** (these are pure and Frappe-free):
   - `ValidationIssue`, `ValidationReport` dataclasses (+ `as_dict`, `errors`,
     `warnings`, `has_errors`, `by_entity`, `summary_lines`).
   - `_GSTIN_RE`, `_GSTIN_CHARS`, `GSTIN_STATE_CODES`, `gstin_check_digit`,
     `validate_gstin`, `infer_gst_category`.
   - `PIN_PREFIX_STATE`, `pin_state_conflict`.
   - `_SUFFIXES`, `normalize_party_name`, `_phone_digits`, `find_duplicate_groups`.
   - `_validate_party`, `validate_masters`.
3. **`validate_masters` input.** Bridge takes its `ExtractedMasters` dataclass.
   Migrator's `ExtractedMasters` (in `tally/extractors.py`) has the **same**
   `.customers / .suppliers / .items` attributes of `list[dict]` keyed by `_name`,
   so `validate_masters(masters, report)` works unchanged.
4. **Drop** `validate_vouchers` and the vouchers branch of `validate_extraction`
   for now (no voucher model yet). Keep `validate_extraction(masters=...)`.

> Net: ~95% copy-paste. The only real edits are the two import lines and removing
> the voucher function.

### 3.3 New API endpoint: `tally_migrator/api.py`

Add one whitelisted method, mirroring `validate_masters_file`:

```python
@frappe.whitelist()
def validate_masters_data(file_url):
    """Pre-flight data-quality scan of an uploaded Tally Masters XML.

    Read-only — extracts and inspects, writes nothing. Returns a grouped,
    UI-ready report so the user can see how dirty the data is and decide
    (fix in Tally / proceed anyway) before any migration runs.
    """
    frappe.only_for(["System Manager", "Tally Migration Manager"])
    from tally_migrator.tally.extractors import TallyExtractor
    from tally_migrator.validation.engine import validate_extraction

    file_doc = frappe.get_doc("File", {"file_url": file_url})
    xml_text = _decode(file_doc.get_content())
    masters = TallyExtractor(FileTallySource(xml_text)).extract_all()
    report = validate_extraction(masters=masters)
    return _group_report(report)   # see §3.4
```

### 3.4 Grouping helper (UI-ready shape)

Per the BUILD_SPEC UX rule "group repetitive issues" (collapse "GST state missing
— 13 suppliers" into one expandable row, not 13 rows). Add to `api.py` (or the
engine) a pure shaper:

```python
def _group_report(report) -> dict:
    """Shape a ValidationReport into grouped-by-code rows for the frontend."""
    groups = {}
    for i in report.issues:
        g = groups.setdefault(i.code, {
            "code": i.code,
            "severity": i.severity,          # "error" | "warning"
            "message": i.message,            # representative; per-row detail below
            "fix_hint": i.fix_hint,
            "items": [],                     # [{entity_type, entity_name, message}]
        })
        g["items"].append({
            "entity_type": i.entity_type,
            "entity_name": i.entity_name,
            "message": i.message,
        })
    ordered = sorted(groups.values(),
                     key=lambda g: (g["severity"] != "error", g["code"]))
    return {
        "totals": report.totals,                       # per entity-type counts
        "error_count": len(report.errors),
        "warning_count": len(report.warnings),
        "clean": not report.issues,
        "groups": ordered,                             # errors first
    }
```

Return shape consumed by the frontend:
```jsonc
{
  "totals": {"Customer": 12, "Supplier": 8, "Item": 40},
  "error_count": 3,
  "warning_count": 14,
  "clean": false,
  "groups": [
    {
      "code": "GST_STATE_MISSING", "severity": "error",
      "message": "...", "fix_hint": "Set the state in Tally...",
      "items": [{"entity_type":"Supplier","entity_name":"Acme","message":"..."}, ...]
    },
    ...
  ]
}
```

---

## 4. Frontend — the pre-flight issues screen

### 4.1 Where it fits in the stepper

Current steps: **Upload → Configure → Check (UOM) → Migrate.**

Proposed: fold data-quality into the existing **Check** step (don't add a 5th
circle — keep the flow short). The Check step renders **two stacked panels**:

1. **Data quality** (new) — grouped issues table.
2. **Units of measure** (existing UOM resolution table).

Both must resolve/acknowledge before **Continue** enables. Call
`validate_masters_data` and `validate_masters_file` together when entering the
step (two parallel `frappe.call`s), render both, then one Continue.

> Alternative if the Check step gets too tall: a sub-tab toggle ("Data quality |
> Units") inside the Check panel. Default to Data quality; badge each tab with its
> unresolved count. Decide during build based on real height.

### 4.2 Issues table (reuse the UOM table styling)

- Compact, scrollable container (`max-height: 340px`, same as UOM table).
- **Stat cards row** at top: `N Errors` (red), `N Warnings` (amber), `Clean`
  (green) — from `error_count` / `warning_count` / `clean`.
- One **expandable row per `code`** (grouped):
  - Collapsed: severity dot · human label (e.g. "GST state missing") · count
    badge (`13 suppliers`) · the `fix_hint`.
  - Expanded: the per-entity list (`entity_type · entity_name`), scrollable if long.
- Errors sorted first (already ordered by `_group_report`).

### 4.3 The permission-respecting decision (core UX constraint)

The user's hard rule: **never auto-fix; tell them clearly, let them decide.**
This engine **cannot** fix the data (the fixes live in Tally), so the screen is
*informational + gating*, not *resolution* like UOMs. Two outcomes:

- **Errors present** → these *will* break the migration for those records. Show a
  clear banner: "3 records have errors that will fail to import. You can fix them
  in Tally and re-upload, or continue and migrate everything else." Continue stays
  enabled (error isolation means the rest still imports) but the primary action is
  framed as informed consent, with an explicit checkbox or secondary-styled
  Continue: **"Continue anyway — skip/flag the 3 problem records"**.
- **Warnings only** → Continue is primary/enabled; warnings are advisory.
- **Clean** → green "No data issues found" panel; Continue primary.

Nothing on this screen writes to ERPNext or Tally. It only decides whether to
proceed. (Contrast with the UOM screen, which *does* create UOMs — because that's
a safe, ERPNext-side, user-approved create.)

### 4.4 Persisting the report to the log

When the migration runs, store the grouped report JSON on the Tally Migration Log
so the post-run record shows what was flagged pre-flight. Add a `validation_report`
(Long Text / JSON, read_only) field to `Tally Migration Log` and render it in the
log form script under a collapsible "Pre-flight data quality" section, reusing the
grouped renderer. (Optional in v1; nice for the audit story.)

---

## 5. Tests

Port `tests/test_validation.py` to `tally_migrator/tests/test_validation.py`
**verbatim** except the import path:
- `from tally_bridge.validation import (...)` →
  `from tally_migrator.validation.engine import (...)`
- The voucher tests (`TestValidateVouchers`) and the `TallyVoucher`/`LedgerEntry`
  imports — **drop** (no voucher model yet).
- `ExtractedMasters` import → `from tally_migrator.tally.extractors import ExtractedMasters`
  (note: Migrator's `ExtractedMasters` requires `warehouses=` — the test helper
  `_masters` already passes `warehouses=[]`, so it's compatible).

These run **without Frappe** (pure logic), so unlike the rest of the suite they're
runnable locally — a rare chance to actually execute tests under the build-only
constraint. Run: `python -m unittest tally_migrator.tests.test_validation`.

Add one new shaper test: `_group_report` collapses N same-code issues into one
group with `len(items) == N`, errors sorted before warnings.

---

## 6. Adjacent enrichment to fold in (cheap, while we're here)

`infer_gst_category(gstin, country)` is in the ported engine. When we touch
`PartyImporter`/`CustomerImporter`/`SupplierImporter.build_doc`, set
`gst_category` from it (and `tax_id` from GSTIN, `pan` from `INCOMETAXNumber` if
not already mapped). This is Gap 5 but it's a one-liner once the engine is present
— worth doing in the same PR so the validation rules and the importer agree on GST
semantics. **Check first** what the current party importers already set (build_doc
in importers.py) to avoid duplication.

---

## 7. Build order

1. Extract `safe_item_code` so validation + import share one slug. (small)
2. Add `tally_migrator/validation/{__init__,engine}.py` (port). (small)
3. Port + trim tests; **run them locally** to prove the port. (small)
4. Add `validate_masters_data` API + `_group_report`. (small)
5. Wire the Check step: parallel call, render grouped table + stat cards, gating
   logic. (medium — the bulk of the work is frontend)
6. (Optional) log field + collapsible render of the pre-flight report.
7. (Optional, Gap 5) gst_category/tax_id/pan enrichment in party importers.

---

## 8. Decisions (locked)

1. **Stepper:** fold data-quality into the existing **Check** step (two stacked
   panels: Data quality + Units). Stays at 4 steps.
2. **Errors gating:** **Continue anyway (informed consent)** — error isolation
   imports the rest; show a clear banner about the N problem records.
3. **Log persistence:** **Yes** — add `validation_report` field to Tally Migration
   Log + collapsible render in the form script (§4.4 is in scope, not optional).
4. **GST enrichment (§6):** **bundle it in** — wire `gst_category`/`tax_id`/`pan`
   into the party importers in the same change (check existing `build_doc` first to
   avoid duplication).
