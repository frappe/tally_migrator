"""Result tracking and the BaseImporter template (shared by all importers)."""

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

    def add_created(self, name: str, doctype: str = "") -> None:
        self.created += 1
        if name:
            self.created_names.append(name)
            self.created_docs.append({"name": name, "doctype": doctype or self.doctype})

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

    @property
    def company_country(self) -> str:
        """The target ERPNext Company's country - the correct default for records
        whose Tally ledger leaves country blank, instead of assuming India."""
        return frappe.get_cached_value("Company", self.company, "country") or "India"

    # ── Template method ─────────────────────────────────────────────────────
    def run(self, records: list[dict]) -> ImportResult:
        result = ImportResult(self.doctype)
        self.before_run(records, result)
        for record in self.iter_records(records):
            name, created = self._upsert(result, self.build_doc(record))
            # after_insert (e.g. address creation) must run ONLY for newly
            # created records - otherwise a re-run duplicates side effects for
            # records that were skipped because they already exist.
            if name and created:
                self.after_insert(name, record, result)
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
        filters = {self.key_field: key_value}
        if self.scope_field:
            filters[self.scope_field] = self.company
        try:
            existing = frappe.db.get_value(self.doctype, filters, "name")
            if existing:
                result.skipped += 1
                return existing, False
            doc = frappe.get_doc(data)
            doc.insert(ignore_permissions=True)
            frappe.db.commit()
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
