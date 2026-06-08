"""
Read-only data-quality validation for extracted Tally masters — the pre-flight
"measure" pass.

Pure logic, no network, no Frappe, no writes. Runs against the dicts that
``TallyExtractor`` already produces (keyed by ``_name`` with Tally field names) and
emits a structured report so a user can see *how dirty the real data is* before any
migration is attempted.

Every rule is offline and dependency-free. Live GSTIN *registry* checks (is this
GSTIN real/active?) need an external API and are intentionally out of scope here;
GSTIN *checksum + structure* validation is fully deterministic and done locally.

Ported from the sibling Tally Bridge app (tally_bridge/validation.py); the voucher
rules are deferred until the transactions feature lands.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from tally_migrator.naming import safe_item_code
from tally_migrator.tally.mappings import TALLY_STATE_MAP

# ── Issue + report model ──────────────────────────────────────────────────────

ERROR = "error"       # would fail the migration / corrupt the books
WARNING = "warning"   # migrates, but a human should look


@dataclass
class ValidationIssue:
    entity_type: str   # "Customer" | "Supplier" | "Item"
    entity_name: str
    severity: str      # ERROR | WARNING
    code: str          # stable machine code, e.g. "GSTIN_INVALID"
    message: str
    fix_hint: str = ""

    def as_dict(self) -> dict:
        return {
            "entity_type": self.entity_type,
            "entity_name": self.entity_name,
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "fix_hint": self.fix_hint,
        }


@dataclass
class ValidationReport:
    issues: list[ValidationIssue] = field(default_factory=list)
    totals: dict[str, int] = field(default_factory=dict)  # entity_type -> found count

    def add(self, issue: ValidationIssue) -> None:
        self.issues.append(issue)

    def count(self, entity_type: str, n: int) -> None:
        self.totals[entity_type] = self.totals.get(entity_type, 0) + n

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == ERROR]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == WARNING]

    @property
    def has_errors(self) -> bool:
        return any(i.severity == ERROR for i in self.issues)

    def by_entity(self, entity_type: str) -> list[ValidationIssue]:
        return [i for i in self.issues if i.entity_type == entity_type]

    def summary_lines(self) -> list[str]:
        """One human line per entity type: found / ok / warnings / errors."""
        lines = []
        for etype, found in self.totals.items():
            err_names = {i.entity_name for i in self.issues
                         if i.entity_type == etype and i.severity == ERROR}
            warn_names = {i.entity_name for i in self.issues
                          if i.entity_type == etype and i.severity == WARNING}
            ok = found - len(err_names)
            parts = [f"{found} found", f"{ok} ok"]
            if warn_names:
                parts.append(f"{len(warn_names)} warning(s)")
            if err_names:
                parts.append(f"{len(err_names)} error(s)")
            lines.append(f"{etype:<10}: " + " · ".join(parts))
        return lines

    def as_dict(self) -> dict:
        return {
            "totals": self.totals,
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "issues": [i.as_dict() for i in self.issues],
        }


# ── GSTIN: structure + checksum (deterministic, offline) ──────────────────────

_GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
_GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"  # mod-36 alphabet

# GST state code (first 2 GSTIN digits) -> canonical ERPNext state name.
GSTIN_STATE_CODES: dict[str, str] = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab",
    "04": "Chandigarh", "05": "Uttarakhand", "06": "Haryana", "07": "Delhi",
    "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur", "15": "Mizoram",
    "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
    "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh",
    "24": "Gujarat", "25": "Daman and Diu", "26": "Dadra and Nagar Haveli",
    "27": "Maharashtra", "29": "Karnataka", "30": "Goa", "31": "Lakshadweep",
    "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh",
    "38": "Ladakh",
}


def gstin_check_digit(first14: str) -> str:
    """Compute the 15th GSTIN character (mod-36 weighted checksum)."""
    factor = 2
    total = 0
    mod = len(_GSTIN_CHARS)
    for ch in reversed(first14):
        val = _GSTIN_CHARS.index(ch)
        addend = factor * val
        factor = 1 if factor == 2 else 2
        addend = (addend // mod) + (addend % mod)
        total += addend
    return _GSTIN_CHARS[(mod - (total % mod)) % mod]


def validate_gstin(gstin: str) -> tuple[bool, str]:
    """Return (is_valid, reason). Empty reason when valid."""
    g = (gstin or "").strip().upper()
    if not g:
        return False, "empty"
    if len(g) != 15:
        return False, f"length {len(g)}, expected 15"
    if not _GSTIN_RE.match(g):
        return False, "does not match GSTIN format"
    if g[:2] not in GSTIN_STATE_CODES:
        return False, f"unknown state code '{g[:2]}'"
    expected = gstin_check_digit(g[:14])
    if g[14] != expected:
        return False, f"checksum mismatch (expected '{expected}')"
    return True, ""


def infer_gst_category(gstin: str, country: str) -> str:
    """Best-effort ERPNext GST Category. Composition/SEZ are NOT encoded in a
    GSTIN, so a structurally valid GSTIN can only be inferred as Registered
    Regular — flagged elsewhere when ambiguous."""
    g = (gstin or "").strip()
    if g:
        ok, _ = validate_gstin(g)
        return "Registered Regular" if ok else "Unregistered"
    c = (country or "India").strip()
    return "Overseas" if c and c != "India" else "Unregistered"


# ── PIN ↔ state (coarse postal-circle prefixes; warnings only) ────────────────
# First two PIN digits map to a postal circle ≈ state. Deliberately coarse — we
# only warn on a confident mismatch, never block, to avoid eroding trust.
PIN_PREFIX_STATE: dict[str, str] = {
    "11": "Delhi", "12": "Haryana", "13": "Haryana", "14": "Punjab",
    "15": "Punjab", "16": "Punjab", "17": "Himachal Pradesh",
    "18": "Jammu and Kashmir", "19": "Jammu and Kashmir",
    "20": "Uttar Pradesh", "21": "Uttar Pradesh", "22": "Uttar Pradesh",
    "23": "Uttar Pradesh", "24": "Uttar Pradesh", "26": "Uttarakhand",
    "27": "Uttarakhand", "28": "Uttar Pradesh",
    "30": "Rajasthan", "31": "Rajasthan", "32": "Rajasthan", "33": "Rajasthan",
    "34": "Rajasthan", "36": "Gujarat", "37": "Gujarat", "38": "Gujarat",
    "39": "Gujarat", "40": "Maharashtra", "41": "Maharashtra", "42": "Maharashtra",
    "43": "Maharashtra", "44": "Maharashtra", "45": "Madhya Pradesh",
    "46": "Madhya Pradesh", "47": "Madhya Pradesh", "48": "Madhya Pradesh",
    "49": "Chhattisgarh", "50": "Telangana", "51": "Andhra Pradesh",
    "52": "Andhra Pradesh", "53": "Andhra Pradesh", "56": "Karnataka",
    "57": "Karnataka", "58": "Karnataka", "59": "Karnataka", "60": "Tamil Nadu",
    "61": "Tamil Nadu", "62": "Tamil Nadu", "63": "Tamil Nadu", "64": "Tamil Nadu",
    "67": "Kerala", "68": "Kerala", "69": "Kerala", "70": "West Bengal",
    "71": "West Bengal", "72": "West Bengal", "73": "West Bengal",
    "74": "West Bengal", "75": "Odisha", "76": "Odisha", "77": "Odisha",
    "78": "Assam", "80": "Bihar", "81": "Bihar", "82": "Bihar", "83": "Jharkhand",
    "84": "Bihar", "85": "Jharkhand",
}


def pin_state_conflict(pin: str, state: str) -> str | None:
    """Return the expected state if PIN clearly disagrees with `state`, else None."""
    p = "".join(filter(str.isdigit, pin or ""))
    if len(p) < 6 or not state:
        return None
    expected = PIN_PREFIX_STATE.get(p[:2])
    if expected and expected != state.strip():
        return expected
    return None


# ── Party de-duplication (normalized name + GSTIN + phone) ────────────────────

_SUFFIXES = {
    "pvt", "private", "ltd", "limited", "llp", "inc", "co", "company",
    "corporation", "corp", "and", "the", "&", "industries", "enterprises",
}


def normalize_party_name(name: str) -> str:
    """Lowercase, strip punctuation and common company suffixes, collapse spaces."""
    s = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    tokens = [t for t in s.split() if t and t not in _SUFFIXES]
    return " ".join(tokens)


def _phone_digits(rec: dict) -> str:
    raw = rec.get("LedgerPhone") or rec.get("LedgerMobile") or ""
    return "".join(filter(str.isdigit, raw))[-10:]  # last 10 digits


def find_duplicate_groups(parties: list[dict], threshold: float = 0.80) -> list[list[str]]:
    """Group party names that are likely the same real entity.

    A pair groups when: identical normalized name, OR same non-empty GSTIN, OR
    same 10-digit phone, OR fuzzy name ratio >= threshold. Returns groups of size
    >= 2 (the singletons are not duplicates).
    """
    n = len(parties)
    norm = [normalize_party_name(p["_name"]) for p in parties]
    gst = [(p.get("GSTRegistrationNumber") or "").strip().upper() for p in parties]
    phone = [_phone_digits(p) for p in parties]

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        parent[find(a)] = find(b)

    for i in range(n):
        for j in range(i + 1, n):
            same = (
                (norm[i] and norm[i] == norm[j])
                or (gst[i] and gst[i] == gst[j])
                or (phone[i] and phone[i] == phone[j])
                or SequenceMatcher(None, norm[i], norm[j]).ratio() >= threshold
            )
            if same:
                union(i, j)

    groups: dict[int, list[str]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(parties[i]["_name"])
    return [sorted(g) for g in groups.values() if len(g) > 1]


# ── Per-entity rule passes ────────────────────────────────────────────────────

def _validate_party(rec: dict, entity_type: str, report: ValidationReport) -> None:
    name = rec["_name"]
    gstin = (rec.get("GSTRegistrationNumber") or "").strip()
    country = (rec.get("CountryName") or "India").strip()
    state = (rec.get("LedgerState") or "").strip()
    pin = rec.get("PinCode") or ""

    if gstin:
        ok, reason = validate_gstin(gstin)
        if not ok:
            report.add(ValidationIssue(
                entity_type, name, ERROR, "GSTIN_INVALID",
                f"GSTIN '{gstin}' is invalid ({reason}).",
                "Correct the GSTIN in Tally, or clear it to migrate as Unregistered."))
        else:
            code_state = GSTIN_STATE_CODES.get(gstin[:2].strip())
            if state and code_state and code_state != state:
                report.add(ValidationIssue(
                    entity_type, name, WARNING, "GSTIN_STATE_MISMATCH",
                    f"GSTIN state code maps to {code_state} but ledger state is {state}.",
                    "Verify the party's state — wrong state flips CGST/SGST vs IGST."))

    # GST state is mandatory for India Compliance, even when unregistered.
    if country == "India" and not state:
        report.add(ValidationIssue(
            entity_type, name, ERROR, "GST_STATE_MISSING",
            "No GST state — ERPNext (India Compliance) requires one on every party.",
            "Set the state in Tally, or derive it from the PIN code."))

    expected = pin_state_conflict(pin, state)
    if expected:
        report.add(ValidationIssue(
            entity_type, name, WARNING, "PIN_STATE_CONFLICT",
            f"PIN {pin} looks like {expected}, but state is {state}.",
            "Check whether the PIN or the state is the typo."))


def _validate_items(items: list[dict], report: ValidationReport) -> None:
    """Per-item rules: HSN presence (warning) + item_code collisions (error)."""
    seen_codes: dict[str, str] = {}
    for it in items:
        name = it["_name"]
        if not (it.get("HSNCode") or "").strip():
            report.add(ValidationIssue(
                "Item", name, WARNING, "HSN_MISSING",
                "No HSN/SAC code — GST invoices for this item won't be compliant.",
                "Add the HSN code in Tally before invoicing."))
        code = safe_item_code(name)
        if code in seen_codes:
            report.add(ValidationIssue(
                "Item", name, ERROR, "ITEM_CODE_COLLISION",
                f"item_code '{code}' collides with '{seen_codes[code]}'.",
                "Rename one item in Tally — ERPNext item codes must be unique."))
        else:
            seen_codes[code] = name


def _validate_duplicates(parties: list[dict], report: ValidationReport,
                         entity_of: dict | None = None) -> None:
    """Flag likely-duplicate parties (customers + suppliers share the namespace)."""
    entity_of = entity_of or {}
    for group in find_duplicate_groups(parties):
        primary = group[0]
        for dupe in group[1:]:
            report.add(ValidationIssue(
                entity_of.get(dupe, "Customer"), dupe, WARNING, "DUPLICATE_PARTY",
                f"Looks like a duplicate of '{primary}'.",
                "Merge in Tally, or pick one survivor before migrating."))


def validate_masters(masters, report: ValidationReport | None = None) -> ValidationReport:
    report = report or ValidationReport()
    report.count("Customer", len(masters.customers))
    report.count("Supplier", len(masters.suppliers))
    report.count("Item", len(masters.items))

    for c in masters.customers:
        _validate_party(c, "Customer", report)
    for s in masters.suppliers:
        _validate_party(s, "Supplier", report)
    _validate_items(masters.items, report)
    entity_of = {c["_name"]: "Customer" for c in masters.customers}
    entity_of.update({s["_name"]: "Supplier" for s in masters.suppliers})
    _validate_duplicates(masters.customers + masters.suppliers, report, entity_of)
    return report


def validate_extraction(masters=None) -> ValidationReport:
    """Run every applicable masters rule over whatever was extracted."""
    report = ValidationReport()
    if masters is not None:
        validate_masters(masters, report)
    return report


# Per-rule fields the user can fix inline on the pre-flight screen. The edits
# become in-memory record overrides (see migration/overrides.py) — the source XML
# is never mutated. ``type`` drives the input widget: "text" or "state" (dropdown).
EDITABLE_FIELDS: dict[str, list[dict]] = {
    "GSTIN_INVALID":        [{"field": "GSTRegistrationNumber", "label": "GSTIN", "type": "text"}],
    "GST_STATE_MISSING":    [{"field": "LedgerState", "label": "State", "type": "state"}],
    "GSTIN_STATE_MISMATCH": [{"field": "LedgerState", "label": "State", "type": "state"},
                             {"field": "GSTRegistrationNumber", "label": "GSTIN", "type": "text"}],
    "PIN_STATE_CONFLICT":   [{"field": "LedgerState", "label": "State", "type": "state"},
                             {"field": "PinCode", "label": "PIN code", "type": "text"}],
    "HSN_MISSING":          [{"field": "HSNCode", "label": "HSN code", "type": "text"}],
}


def erpnext_states() -> list[str]:
    """Canonical ERPNext state names, for the inline 'State' dropdown."""
    return sorted(set(TALLY_STATE_MAP.values()))


def records_by_key(masters) -> dict:
    """Index extracted records by (entity_type, name) for current-value lookups."""
    index: dict = {}
    for c in masters.customers:
        index[("Customer", c["_name"])] = c
    for s in masters.suppliers:
        index[("Supplier", s["_name"])] = s
    for it in masters.items:
        index[("Item", it["_name"])] = it
    return index


def group_report(report: ValidationReport, lookup: dict | None = None) -> dict:
    """Shape a ValidationReport into grouped-by-code rows for the frontend.

    Collapses repetitive issues (e.g. "GST state missing — 13 suppliers") into one
    expandable group instead of N rows. Errors are ordered before warnings.

    When ``lookup`` (from :func:`records_by_key`) is supplied, each group carries
    its ``editable_fields`` and each item gets a ``current`` map of those fields'
    present values, so the screen can render pre-filled inline editors.
    """
    groups: dict = {}
    for i in report.issues:
        g = groups.setdefault(i.code, {
            "code": i.code,
            "severity": i.severity,        # "error" | "warning"
            "fix_hint": i.fix_hint,
            "editable_fields": EDITABLE_FIELDS.get(i.code, []),
            "items": [],                   # [{entity_type, entity_name, message, current?}]
        })
        item = {
            "entity_type": i.entity_type,
            "entity_name": i.entity_name,
            "message": i.message,
        }
        if lookup is not None and i.code in EDITABLE_FIELDS:
            record = lookup.get((i.entity_type, i.entity_name), {})
            item["current"] = {
                f["field"]: (record.get(f["field"]) or "")
                for f in EDITABLE_FIELDS[i.code]
            }
        g["items"].append(item)
    ordered = sorted(groups.values(), key=lambda g: (g["severity"] != "error", g["code"]))
    return {
        "totals": report.totals,
        "error_count": len(report.errors),
        "warning_count": len(report.warnings),
        "clean": not report.issues,
        "groups": ordered,
    }
