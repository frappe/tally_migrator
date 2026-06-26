"""Result tracking and the BaseImporter template (shared by all importers)."""

import contextlib
import unicodedata
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

# ── Result tracking ───────────────────────────────────────────────────────────

@dataclass
class ImportResult:
    doctype: str
    created: int = 0
    skipped: int = 0
    errors: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    # ERPNext names of the docs this importer actually inserted - the authoritative
    # "what did this run touch" record (incl. the opening JE / Stock Reconciliation),
    # so a migration can be reviewed or reversed by inspection.
    created_names: list[str] = field(default_factory=list)
    # Same docs, each tagged with its real ERPNext doctype, so the migration log can
    # deep-link them. A single importer can create more than one doctype (party
    # openings -> Sales/Purchase Invoice + Payment Entry; an account -> its Bank
    # Account), so the doctype can't be inferred from the importer's label alone.
    created_docs: list[dict] = field(default_factory=list)

    def add_created(self, name: str, doctype: str = "", label: str = "") -> None:
        self.created += 1
        if name:
            self.created_names.append(name)
            entry = {"name": name, "doctype": doctype or self.doctype}
            # A human-readable label for docs whose `name` is an opaque autoname (e.g.
            # an Item Price hashes to "ajq2cf6vcn"); the log shows `label` but still
            # links via `name`. Omitted when the name is already meaningful.
            if label:
                entry["label"] = label
            self.created_docs.append(entry)

    @staticmethod
    def _sentence(reason) -> str:
        """Normalise an issue message to sentence case (capital first letter) so every
        row in the log's issues table reads consistently, no matter how its call site
        phrased it. Enforced here, once, rather than policed across ~40 call sites."""
        s = str(reason).strip()
        return s[:1].upper() + s[1:] if s else s

    def add_error(self, name: str, reason) -> None:
        self.errors.append({"name": name, "reason": self._sentence(reason)})

    def add_warning(self, name: str, reason) -> None:
        """Record a *non-fatal* partial drop - the main record imported, but a
        dependent piece (e.g. its address) was lost. Surfaced in the log so the
        loss is visible/auditable, but it does not mark the record as failed."""
        self.warnings.append({"name": name, "reason": self._sentence(reason)})

    @property
    def failed(self) -> int:
        return len(self.errors)

    @property
    def warned(self) -> int:
        return len(self.warnings)

    def as_dict(self) -> dict:
        return {
            "created": self.created,
            "skipped": self.skipped,
            "failed": self.failed,
            "warned": self.warned,
            "errors": self.errors,
            "warnings": self.warnings,
        }


# ── Base importer ──────────────────────────────────────────────────────────────

class BaseImporter:
    """
    Template for importing one entity type.

    Subclasses set ``doctype`` / ``key_field`` and implement ``build_doc``.
    Optional hooks: ``iter_records`` (ordering), ``before_run`` (prerequisites),
    ``after_insert`` (side effects such as addresses).
    """

    doctype: str = ""
    key_field: str = ""
    # When set, the duplicate-detection lookup is also filtered by this field =
    # ``self.company``. Required for company-scoped doctypes (e.g. Warehouse), where
    # the same ``key_field`` value can legitimately exist in another company -
    # without it, a same-named record in Company A makes Company B's get skipped.
    scope_field: str = ""

    def __init__(self, company: str, abbr: str):
        self.company = company
        self.abbr = abbr
        # Set by run() to a {key_value: name} map of records that already exist, so the
        # per-record duplicate check is an in-memory lookup instead of a SQL query. None
        # means "not prefetched" - _upsert then falls back to a live get_value. See
        # _prefetch_existing / _upsert.
        self._existing: dict | None = None

    @property
    def company_country(self) -> str:
        """The target ERPNext Company's country - the correct default for records
        whose Tally ledger leaves country blank, instead of assuming India."""
        return frappe.get_cached_value("Company", self.company, "country") or "India"

    # ── Template method ─────────────────────────────────────────────────────
    def run(self, records: list[dict], on_progress=None) -> ImportResult:
        result = ImportResult(self.doctype)
        self.before_run(records, result)
        self._existing = self._prefetch_existing()
        # ``on_progress(done, total)`` lets the caller draw a live progress bar that
        # moves *within* this phase (not just between phases). Called once per record
        # - skips included - so the bar advances even on an all-skip re-run. ``total``
        # is the input count; ``iter_records`` only filters, never multiplies, so
        # ``done`` never exceeds it. Optional: tests and direct callers pass nothing.
        total = len(records)
        for done, record in enumerate(self.iter_records(records), 1):
            name, created = self._upsert(result, self.build_doc(record))
            # after_insert (e.g. address creation) must run ONLY for newly
            # created records - otherwise a re-run duplicates side effects for
            # records that were skipped because they already exist.
            if name and created:
                self.after_insert(name, record, result)
            if on_progress:
                on_progress(done, total)
        return result

    # ── Overridable hooks ────────────────────────────────────────────────────
    def iter_records(self, records: list[dict]) -> list[dict]:
        return records

    def before_run(self, records: list[dict], result: ImportResult) -> None:
        pass

    def build_doc(self, record: dict) -> dict:
        raise NotImplementedError

    def after_insert(self, name: str, record: dict, result: "ImportResult") -> None:
        pass

    # ── Duplicate-check prefetch ───────────────────────────────────────────────
    @staticmethod
    def _dedupe_key(value) -> str:
        """A *prefilter* key for the duplicate check - NOT the authority.

        The dedup columns (supplier_name, customer_name, item_code, warehouse_name) are
        ``utf8mb4_unicode_ci``: case-, accent- and (PAD SPACE) trailing-space-insensitive
        - verified empirically to collapse even 'ABC'/'abc', 'café'/'cafe', 'ß'/'ss',
        ligatures and full-width forms. We approximate that with NFKD accent-strip +
        casefold + trailing-space strip, chosen to be at least as aggressive as the
        collation so a *miss* here reliably means "no DB variant exists" (safe to insert
        without a query). It is never trusted to *skip*: an exact match skips directly,
        and any normalised-only match is confirmed against the DB (see _lookup_existing),
        so an imperfect approximation can never drop a distinct record."""
        s = unicodedata.normalize("NFKD", str(value if value is not None else ""))
        s = "".join(c for c in s if not unicodedata.combining(c))
        return s.casefold().rstrip(" ")

    def _prefetch_existing(self) -> dict | None:
        """Snapshot existing records once, so the per-record duplicate check in ``_upsert``
        avoids a SQL round-trip per record.

        Why this matters: ``key_field`` (e.g. ``supplier_name``) is usually not indexed,
        so a per-record ``get_value`` is a full table scan - O(n) per record, O(n^2) over
        the run, and the dominant cost on large books and on every re-run (all skips).

        Returns ``{"exact": {raw_key: name}, "norm": {normalised_key, ...}}`` so
        ``_lookup_existing`` can:
          - skip instantly on an exact key match (an exact match is always equal under the
            collation, so this never drops a distinct record), and
          - tell, from the normalised set, whether a case/accent/space *variant* might
            exist and therefore needs a DB confirmation.

        Returns ``None`` (disabling the optimisation; ``_upsert`` then uses the live
        lookup) when there is no ``key_field`` to key on.

        Note on concurrency: this is a per-run snapshot, kept current as this run inserts.
        It can only go stale if a *second* migration writes the same company at the same
        time, which the single-active-run guard (``_assert_no_active_run`` / the wizard's
        reconnect) already prevents - so the snapshot is safe in normal operation, and a
        variant collision would in any case be caught by the DB confirmation."""
        if not self.key_field:
            return None
        filters = {}
        if self.scope_field:
            filters[self.scope_field] = self.company
        exact: dict = {}
        norm: set = set()
        for row in frappe.get_all(
                self.doctype, filters=filters, fields=[self.key_field, "name"]):
            kv = row.get(self.key_field)
            exact[kv] = row.get("name")
            norm.add(self._dedupe_key(kv))
        return {"exact": exact, "norm": norm}

    def _lookup_existing(self, key_value) -> str | None:
        """Name of an already-existing record equal to ``key_value`` under the DB
        collation, or ``None``. Over-skip-proof: a skip is returned only on an exact key
        match (always collation-equal) or a DB-confirmed variant match; a brand-new key
        (no exact and no normalised hit) returns ``None`` without a query. So the common
        exact and brand-new cases never hit the DB, and only a genuine case/accent/space
        variant collision triggers one authoritative ``get_value``."""
        if self._existing is None:
            filters = {self.key_field: key_value}
            if self.scope_field:
                filters[self.scope_field] = self.company
            return frappe.db.get_value(self.doctype, filters, "name")
        if key_value in self._existing["exact"]:
            return self._existing["exact"][key_value]
        if self._dedupe_key(key_value) in self._existing["norm"]:
            filters = {self.key_field: key_value}
            if self.scope_field:
                filters[self.scope_field] = self.company
            return frappe.db.get_value(self.doctype, filters, "name")
        return None

    def _remember_inserted(self, key_value, name: str) -> None:
        """Add a just-inserted record to the in-run snapshot so a later record with the
        same (or variant) key in this same run is skipped - mirroring the old behaviour
        where the just-committed row was found by the live lookup."""
        if self._existing is not None:
            self._existing["exact"][key_value] = name
            self._existing["norm"].add(self._dedupe_key(key_value))

    # ── Shared upsert ─────────────────────────────────────────────────────────
    def _upsert(self, result: ImportResult, data: dict) -> tuple[str | None, bool]:
        """
        Insert ``data`` unless a record with the same ``key_field`` exists.

        Returns ``(name, created)``:
        - ``(existing_name, False)`` when skipped (already present),
        - ``(new_name, True)``      when newly inserted,
        - ``(None, False)``         when the insert failed.

        The ``created`` flag lets ``run`` fire ``after_insert`` side effects
        only for genuinely new records (idempotent re-runs).

        Throughput vs. atomicity (deliberate)
        -------------------------------------
        This commits once per record, so a run is *not* one transaction: an
        interrupted run leaves every record committed so far in place. That is
        intentional - the import is idempotent (existing records are skipped), so
        a resumed/re-run picks up exactly where it stopped instead of redoing
        thousands of rows or rolling them all back. The cost is one COMMIT plus a
        duplicate-check round-trip per record, which is the dominant per-row cost
        at scale; batching commits would trade resumability for speed and is a
        deliberate non-goal here.
        """
        key_value = data.get(self.key_field, "")
        try:
            # Already exists (under the DB collation)? Skip. _lookup_existing is
            # over-skip-proof: it only ever reports a match it is certain of (exact key,
            # or a DB-confirmed variant), so this can never drop a genuinely new record.
            existing = self._lookup_existing(key_value)
            if existing:
                result.skipped += 1
                return existing, False
            doc = frappe.get_doc(data)
            doc.insert(ignore_permissions=True)
            frappe.db.commit()
            self._remember_inserted(key_value, doc.name)
            result.add_created(doc.name)
            return doc.name, True
        except Exception as exc:
            frappe.db.rollback()
            # Give the importer one chance to salvage the row (e.g. drop an
            # India-Compliance-rejected HSN code) rather than lose it outright.
            recovered = self.recover_insert(data, exc)
            if recovered is not None:
                retry_data, warning = recovered
                try:
                    doc = frappe.get_doc(retry_data)
                    doc.insert(ignore_permissions=True)
                    frappe.db.commit()
                    self._remember_inserted(retry_data.get(self.key_field, key_value), doc.name)
                    result.add_created(doc.name)
                    if warning:
                        result.add_warning(retry_data.get(self.key_field, key_value), warning)
                    return doc.name, True
                except Exception:
                    frappe.db.rollback()
            result.add_error(key_value, exc)
            return None, False

    def recover_insert(self, data: dict, exc: Exception):
        """Hook: return ``(modified_doc, warning)`` to retry the insert once after a
        failure, or ``None`` to record the failure as-is. The warning is logged only
        when the retry succeeds. Default: no recovery."""
        return None

    # ── Utilities ─────────────────────────────────────────────────────────────
    @staticmethod
    def _to_float(val) -> float:
        try:
            return float(str(val or 0).replace(",", "").strip())
        except (ValueError, TypeError):
            return 0.0
