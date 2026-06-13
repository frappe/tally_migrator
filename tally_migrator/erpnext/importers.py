"""
ERPNext importers - Tally masters → ERPNext via Frappe's ORM.

Design
------
A small class hierarchy, one importer per entity, behind the ``ERPNextImporter``
facade::

    BaseImporter                     shared upsert / utilities / template run()
    ├── PartyImporter                shared address + payment-term handling
    │   ├── CustomerImporter
    │   └── SupplierImporter
    ├── ItemImporter                 ensures Item Groups, maps UOM
    └── WarehouseImporter            parent-before-child topological order

Adding a new entity in Phase 2 (e.g. Account, Cost Center) means adding one
subclass - no existing code changes (Open/Closed).

Insert rules (shared by every importer)
---------------------------------------
- Record already exists (matched by ``key_field``)  → skip, never overwrite.
- Insert fails                                       → record error, rollback, continue.
- Per-record commit isolates partial failures so one bad record cannot undo
  successfully imported ones.
"""
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

    def add_error(self, name: str, reason) -> None:
        self.errors.append({"name": name, "reason": str(reason)})

    def add_warning(self, name: str, reason) -> None:
        """Record a *non-fatal* partial drop - the main record imported, but a
        dependent piece (e.g. its address) was lost. Surfaced in the log so the
        loss is visible/auditable, but it does not mark the record as failed."""
        self.warnings.append({"name": name, "reason": str(reason)})

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


# ── Bank Account helpers (shared by party + company bank accounts) ─────────────

def _ensure_bank(bank_name: str) -> str:
    """Return an existing/created Bank master name, or "" when no name is given.

    ERPNext's Bank Account requires a linked Bank; Tally only gives us its name,
    so we create the Bank master on demand. Best-effort: a failure returns "" and
    the caller skips bank-account creation with a warning rather than aborting."""
    name = (bank_name or "").strip()
    if not name:
        return ""
    if frappe.db.exists("Bank", name):
        return name
    try:
        frappe.get_doc({"doctype": "Bank", "bank_name": name}).insert(ignore_permissions=True)
        frappe.db.commit()
        return name
    except Exception:
        frappe.db.rollback()
        return ""


def _insert_bank_account(*, account_name: str, bank: str, account_no: str,
                         ifsc: str, result: "ImportResult", warn_name: str,
                         party_type: str = "", party: str = "",
                         gl_account: str = "", is_company: bool = False,
                         count_created: bool = False) -> str:
    """Insert one Bank Account doc, shared by the party and company bank paths.

    Both paths build the same doc (account name + bank + account no + IFSC) and
    differ only in how it's linked: a party account points at a Customer/Supplier
    (``party_type``/``party``), a company account at the GL account and sets
    ``is_company_account`` (``gl_account``/``is_company``). Non-fatal - a failure
    is logged, recorded as a warning, rolled back, and "" returned so a Bank
    Account quirk never aborts the party/account that was just created.
    """
    try:
        ba = frappe.new_doc("Bank Account")
        ba.account_name = account_name
        ba.bank = bank
        ba.bank_account_no = account_no
        if ifsc:
            ba.branch_code = ifsc
        if is_company:
            ba.account = gl_account
            ba.is_company_account = 1
        else:
            ba.party_type = party_type
            ba.party = party
        ba.insert(ignore_permissions=True)
        frappe.db.commit()
        if count_created:
            result.add_created(ba.name, "Bank Account")
        return ba.name
    except Exception as exc:
        frappe.log_error("Tally Migrator", f"Bank account save failed for {warn_name}: {exc}")
        result.add_warning(warn_name, f"bank account not created: {exc}")
        frappe.db.rollback()
        return ""


# ── Party importers (Customer / Supplier) ──────────────────────────────────────

class PartyImporter(BaseImporter):
    """Shared behaviour for Customers and Suppliers: billing address + payment terms."""

    def after_insert(self, name: str, record: dict, result: "ImportResult") -> None:
        self._save_address(name, self.doctype, record, result)
        self._save_extra_addresses(name, self.doctype, record, result)
        self._save_contact(name, self.doctype, record, result)
        self._save_extra_contacts(name, self.doctype, record, result)
        self._save_bank_account(name, self.doctype, record, result)

    # ERPNext Address.address_type select options - a Tally address-book label
    # (ADDRESSNAME) that matches one is reused, else the address is typed "Other"
    # with the label preserved in its title.
    _ADDRESS_TYPES = {"billing", "shipping", "office", "personal", "plant", "postal",
                      "shop", "subsidiary", "warehouse", "current", "permanent", "other"}

    def _save_extra_addresses(self, link_name: str, link_type: str, data: dict,
                              result: "ImportResult") -> None:
        """Create the party's *additional* addresses (its Tally address book).

        The primary mailing address is created by ``_save_address``; a real export
        also carries an address book (``LEDMULTIADDRESSLIST.LIST``) the extractor
        attaches as ``_extra_addresses`` ([{address, name, state, pincode}]). Each
        becomes its own ERPNext Address linked to the party. Non-fatal per address."""
        for a in data.get("_extra_addresses") or []:
            text = (a.get("address") or "").strip()
            if not text:
                continue
            label = (a.get("name") or "").strip()
            try:
                addr = frappe.new_doc("Address")
                addr.address_title = f"{link_name} - {label}" if label else link_name
                addr.address_type = (label.title()
                                     if label.lower() in self._ADDRESS_TYPES else "Other")
                addr.address_line1 = text
                addr.city = "Not Specified"
                row_pin = (a.get("pincode") or "").strip()
                if row_pin:
                    addr.pincode = row_pin
                addr.state = self._extra_address_state(a, data)
                addr.country = (data.get("CountryName") or "").strip() or self.company_country
                addr.append("links", {"link_doctype": link_type, "link_name": link_name})
                addr.insert(ignore_permissions=True)
                frappe.db.commit()
            except Exception as exc:
                frappe.log_error("Tally Migrator", f"Extra address failed for {link_name}: {exc}")
                result.add_warning(link_name, f"additional address not created: {exc}")

    def _extra_address_state(self, row: dict, data: dict) -> str:
        """ERPNext state for an address-book row, most-precise signal first:

        1. the row's OWN state (a real export rarely sets it, but use it when present);
        2. else derive from the row's OWN pincode (India Compliance's pincode<->state
           map - so the derived state is guaranteed consistent with the pincode and
           passes validation);
        3. else inherit the PARTY's state (the only signal a typical export carries -
           and in practice the branch is usually in the party's own state).

        India Compliance requires a state on an Indian address, so this never returns
        empty for a party that has one - keeping the address rather than dropping it."""
        own = TALLY_STATE_MAP.get((row.get("state") or "").strip(), "")
        if own:
            return own
        from_pin = self._state_from_pincode((row.get("pincode") or "").strip())
        if from_pin:
            return from_pin
        return self._resolve_state(data)

    @staticmethod
    def _state_from_pincode(pincode: str) -> str:
        """ERPNext state whose pincode range covers this PIN's first 3 digits, using
        India Compliance's own ``STATE_PINCODE_MAPPING`` (so the result matches IC's
        validation). Returns "" when IC is absent or the PIN maps to no state."""
        pin = "".join(filter(str.isdigit, pincode or ""))
        if len(pin) < 3:
            return ""
        try:
            from india_compliance.gst_india.constants import STATE_PINCODE_MAPPING
        except Exception:
            return ""
        prefix = int(pin[:3])
        for state, ranges in STATE_PINCODE_MAPPING.items():
            # A value is either a single (lo, hi) range or a tuple of such ranges.
            spans = ranges if isinstance(ranges[0], (tuple, list)) else (ranges,)
            if any(lo <= prefix <= hi for lo, hi in spans):
                return state
        return ""

    def _save_extra_contacts(self, link_name: str, link_type: str, data: dict,
                             result: "ImportResult") -> None:
        """Create the party's *additional* named phone contacts.

        The primary contact is created by ``_save_contact``; a real export also
        carries extra named numbers (``CONTACTDETAILS.LIST``) the extractor attaches
        as ``_extra_contacts`` ([{name, phone, whatsapp}]). Each becomes its own
        ERPNext Contact linked to the party, with the WhatsApp-default number marked
        the primary mobile. Non-fatal per contact."""
        for c in data.get("_extra_contacts") or []:
            phone = (c.get("phone") or "").strip()
            if not phone:
                continue
            try:
                contact = frappe.new_doc("Contact")
                contact.first_name = (c.get("name") or "").strip() or link_name
                contact.append("phone_nos", {
                    "phone": phone,
                    "is_primary_mobile_no": 1 if c.get("whatsapp") else 0,
                })
                contact.append("links", {"link_doctype": link_type, "link_name": link_name})
                contact.insert(ignore_permissions=True)
                frappe.db.commit()
            except Exception as exc:
                frappe.log_error("Tally Migrator", f"Extra contact failed for {link_name}: {exc}")
                result.add_warning(link_name, f"additional contact not created: {exc}")

    def _gst_category(self, record: dict) -> str:
        """ERPNext GST Category. Tally's explicit registration type wins when set
        (it alone distinguishes Composition / SEZ). Otherwise the category is India-
        specific: a party outside India is 'Overseas', and only an Indian party has
        its GSTIN inspected. A blank Tally country falls back to the *company's*
        country, not a hardcoded 'India', so a non-Indian book isn't mislabelled."""
        explicit = gst_category_from_type(record.get("GSTRegistrationType") or "")
        if explicit:
            return explicit
        country = (record.get("CountryName") or self.company_country or "India").strip()
        if country.lower() != "india":
            return "Overseas"
        return infer_gst_category(record.get("GSTRegistrationNumber") or "", "India")

    def _maybe_gstin(self, record: dict) -> dict:
        """India Compliance owns the party-level ``gstin`` field and recomputes
        ``gst_category`` from it on validate. If we set ``tax_id`` but not ``gstin``,
        IC sees no registered GSTIN and clobbers our category to 'Unregistered' (and
        the party stores no GSTIN at all). So when the field exists (IC installed)
        and the GSTIN is structurally valid, set it too - which makes IC keep the
        category we computed. An invalid or absent GSTIN falls back to ``tax_id``
        only, exactly as before IC, so a bad GSTIN never blocks the party."""
        gstin = (record.get("GSTRegistrationNumber") or "").strip().upper()
        if (gstin and validate_gstin(gstin)[0]
                and frappe.get_meta(self.doctype).has_field("gstin")):
            return {"gstin": gstin}
        return {}

    def _resolve_payment_terms(self, tally_credit_period: str) -> str:
        """
        Tally stores a credit period as '30 Days' or '30'. The Customer/Supplier
        ``payment_terms`` field links to a **Payment Terms Template**, so map to a
        template named 'Net <days>' when one exists (else leave blank).
        """
        if not tally_credit_period:
            return ""
        days = "".join(filter(str.isdigit, tally_credit_period))
        if not days:
            return ""
        candidate = f"Net {days}"
        return candidate if frappe.db.exists("Payment Terms Template", candidate) else ""

    def _save_address(self, link_name: str, link_type: str, data: dict,
                      result: "ImportResult") -> None:
        """Create a Billing Address linked to the party. Non-fatal on failure -
        but a failure is recorded as a warning so the dropped address is visible
        in the migration log rather than lost silently."""
        raw_address = (data.get("Address") or "").strip()
        if not raw_address:
            return
        _msg_mark = None   # message-queue length, set just before insert (see below)
        try:
            addr = frappe.new_doc("Address")
            addr.address_title = (data.get("MailingName") or "").strip() or link_name
            addr.address_type = "Billing"
            addr.address_line1 = raw_address
            # ERPNext requires a city, but Tally's party ledger has no city field
            # (the PIN has its own field below). Use a real city if one ever appears,
            # otherwise a clear placeholder - never the PIN, which only looked like a
            # city and produced visibly wrong addresses.
            addr.city = (data.get("City") or "").strip() or "Not Specified"
            addr.state = self._resolve_state(data)
            addr.country = (data.get("CountryName") or "").strip() or self.company_country
            addr.pincode = data.get("PinCode") or ""
            addr.phone = data.get("LedgerPhone") or data.get("LedgerMobile") or ""
            addr.email_id = data.get("LedgerEmail") or ""
            # Only set a structurally valid GSTIN: India Compliance validates the
            # address GSTIN and rejects the whole address on a malformed one, which
            # would lose the address entirely. A bad GSTIN is already flagged by the
            # pre-flight; here we drop just the field and keep the address.
            gstin = (data.get("GSTRegistrationNumber") or "").strip().upper()
            addr.gstin = gstin if (gstin and validate_gstin(gstin)[0]) else ""
            addr.append("links", {"link_doctype": link_type, "link_name": link_name})
            # Remember the message-queue length so that, if the insert fails on a
            # pincode/state mismatch, we can drop India Compliance's own "Invalid
            # Postal Code" msgprint (queued before it raised) on the salvage path -
            # otherwise dozens of them surface in a dialog for an address we DID keep.
            try:
                _msg_mark = len(frappe.local.message_log)
            except Exception:
                _msg_mark = None
            addr.insert(ignore_permissions=True)
            frappe.db.commit()
        except Exception as exc:
            # India Compliance hard-rejects a pincode whose leading digits don't match
            # the state ("Postal Code X ... is not associated with <State>"). The
            # pre-flight already warns "PIN and state to verify", so rather than lose
            # the whole address, drop just the suspect PIN and retry once - mirroring
            # how we drop a rejected GSTIN above. Keeps the address; flags the PIN.
            msg = str(exc).lower()
            if addr.pincode and ("not associated with" in msg or "postal code" in msg):
                frappe.db.rollback()
                try:
                    addr.pincode = ""
                    addr.insert(ignore_permissions=True)
                    frappe.db.commit()
                    # Drop the failed attempt's queued "Invalid Postal Code" message -
                    # the address was salvaged, so that warning would only be noise.
                    if _msg_mark is not None:
                        try:
                            del frappe.local.message_log[_msg_mark:]
                        except Exception:
                            pass
                    result.add_warning(
                        link_name, "address imported without its PIN code - the PIN did "
                        "not match the state (verify and set it in ERPNext)")
                    return
                except Exception as exc2:
                    exc = exc2
            frappe.db.rollback()
            frappe.log_error("Tally Migrator", f"Address save failed for {link_name}: {exc}")
            result.add_warning(link_name, f"address not created: {exc}")

    @staticmethod
    def _resolve_state(data: dict) -> str:
        """The party's ERPNext state. Prefer Tally's ledger state; when it's blank
        but the party has a structurally valid GSTIN, derive the state from the
        GSTIN's state code - the same fallback the pre-flight check assumes, so a
        registered party never lands with an empty (and GST-breaking) state."""
        state = TALLY_STATE_MAP.get((data.get("LedgerState") or "").strip(), "")
        if state:
            return state
        gstin = (data.get("GSTRegistrationNumber") or "").strip().upper()
        if gstin and validate_gstin(gstin)[0]:
            return GSTIN_STATE_CODES.get(gstin[:2], "")
        return ""

    def _save_contact(self, link_name: str, link_type: str, data: dict,
                      result: "ImportResult") -> None:
        """Create a Contact (phone / mobile / email) linked to the party.

        Tally keeps these on the ledger, but ERPNext stores them on a Contact, not
        on the Customer/Supplier itself - so without this they'd survive only as
        Address fields and be lost entirely when the party has no street address.
        Non-fatal: a failure is recorded as a warning so the dropped contact is
        visible in the migration log rather than lost silently."""
        phone = (data.get("LedgerPhone") or "").strip()
        mobile = (data.get("LedgerMobile") or "").strip()
        email = (data.get("LedgerEmail") or "").strip()
        email_cc = (data.get("EmailCC") or "").strip()
        if not (phone or mobile or email or email_cc):
            return
        try:
            contact = frappe.new_doc("Contact")
            # Tally's contact-person name when supplied, else the ledger name.
            contact.first_name = (data.get("LedgerContact") or "").strip() or link_name
            if email:
                contact.append("email_ids", {"email_id": email, "is_primary": 1})
            if email_cc and email_cc.lower() != email.lower():
                contact.append("email_ids", {"email_id": email_cc, "is_primary": 0})
            if mobile:
                contact.append("phone_nos", {"phone": mobile, "is_primary_mobile_no": 1})
            if phone:
                contact.append("phone_nos", {
                    "phone": phone,
                    "is_primary_phone": 1 if not mobile else 0,
                })
            contact.append("links", {"link_doctype": link_type, "link_name": link_name})
            contact.insert(ignore_permissions=True)
            frappe.db.commit()
        except Exception as exc:
            frappe.log_error("Tally Migrator", f"Contact save failed for {link_name}: {exc}")
            result.add_warning(link_name, f"contact not created: {exc}")

    def _save_bank_account(self, link_name: str, link_type: str, data: dict,
                           result: "ImportResult") -> None:
        """Create a Bank Account (account no + IFSC) linked to the party.

        Tally stores a party's bank details on the ledger; ERPNext keeps them on a
        Bank Account doc linked to the Customer/Supplier. Non-fatal - a failure is a
        warning, so the dropped bank detail is visible in the log, not lost."""
        acc_no = (data.get("BankAccountNo") or "").strip()
        if not acc_no:
            return
        bank = _ensure_bank(data.get("BankName") or "")
        if not bank:
            result.add_warning(
                link_name, "bank account not created: no bank name on the ledger")
            return
        _insert_bank_account(
            account_name=(data.get("BankAccountHolder") or "").strip() or link_name,
            bank=bank,
            account_no=acc_no,
            ifsc=(data.get("BankIFSC") or "").strip(),
            party_type=link_type,
            party=link_name,
            result=result,
            warn_name=link_name,
        )


class CustomerImporter(PartyImporter):
    doctype = "Customer"
    key_field = "customer_name"

    def build_doc(self, record: dict) -> dict:
        doc = {
            "doctype": "Customer",
            "customer_name": record["_name"],
            "customer_group": DEFAULT_CUSTOMER_GROUP,
            "territory": DEFAULT_TERRITORY,
            "customer_type": "Company",
            "tax_id": record.get("GSTRegistrationNumber") or "",
            "pan": record.get("INCOMETAXNumber") or "",
            "gst_category": self._gst_category(record),
            "payment_terms": self._resolve_payment_terms(record.get("BillCreditPeriod")),
        }
        # Tally's per-ledger credit limit → ERPNext's company-scoped credit_limits.
        limit = self._to_float(record.get("CreditLimit"))
        if limit > 0:
            doc["credit_limits"] = [{"company": self.company, "credit_limit": limit}]
        doc.update(self._maybe_gstin(record))
        return doc


class SupplierImporter(PartyImporter):
    doctype = "Supplier"
    key_field = "supplier_name"

    def build_doc(self, record: dict) -> dict:
        doc = {
            "doctype": "Supplier",
            "supplier_name": record["_name"],
            "supplier_group": DEFAULT_SUPPLIER_GROUP,
            "supplier_type": "Company",
            "tax_id": record.get("GSTRegistrationNumber") or "",
            "pan": record.get("INCOMETAXNumber") or "",
            "gst_category": self._gst_category(record),
            "payment_terms": self._resolve_payment_terms(record.get("BillCreditPeriod")),
        }
        doc.update(self._maybe_gstin(record))
        return doc


# ── Item importer ───────────────────────────────────────────────────────────────

class ItemImporter(BaseImporter):
    doctype = "Item"
    key_field = "item_code"

    def __init__(self, company: str, abbr: str, uom_overrides: dict | None = None):
        super().__init__(company, abbr)
        self._uom_overrides = uom_overrides or {}

    def before_run(self, records: list[dict], result: ImportResult) -> None:
        self._ensure_item_groups({r.get("Parent") for r in records if r.get("Parent")}, result)
        # Two distinct Tally items can collapse to the same ERPNext item_code once
        # it is truncated to 140 chars and '/' is replaced. The generic upsert keys
        # on item_code, so the later one is skipped as "already there" - which looks
        # like a harmless duplicate. Flag the collision here so the dropped item is
        # visible in the log, matching the pre-flight ITEM_CODE_COLLISION check.
        for code, names in self._code_collisions(records).items():
            kept, dropped = names[0], names[1:]
            for name in dropped:
                result.add_warning(
                    name,
                    f"item not imported - its code '{code}' collides with item "
                    f"'{kept}' (both reduce to the same ERPNext item_code after "
                    "truncation/'/' replacement). Rename one in Tally and re-run.")
        # Surface any GST treatment we couldn't map so the loss is auditable rather
        # than silently defaulting the item to taxable.
        for r in records:
            raw = (r.get("GSTTaxability") or "").strip()
            if raw and self._gst_treatment(raw) is None:
                result.add_warning(
                    r["_name"],
                    f"GST type '{raw}' not recognised - item imported as taxable; "
                    "set its GST treatment manually if needed.")
        # India Compliance check: the item-level GST fields (HSN code, taxability,
        # supply type, nil/exempt/non-GST flags) only exist when the India Compliance
        # app is installed. Without it ERPNext core silently drops those keys, so we
        # say so once instead of pretending the GST data landed.
        self._india_compliance = "india_compliance" in frappe.get_installed_apps()
        if not self._india_compliance and any(
            (r.get("GSTTaxability") or r.get("HSNCode")
             or r.get("GstApplicable") or r.get("TypeOfSupply"))
            for r in records
        ):
            result.add_warning(
                "GST details",
                "Your items carry GST data (HSN code, taxability, supply type), but the "
                "India Compliance app is not installed on this site - ERPNext core has no "
                "fields to store it, so these attributes were skipped. To import them, "
                "install 'India Compliance' from the Frappe Cloud marketplace and re-run "
                "this migration; everything else imported normally.")

    @staticmethod
    def _code_collisions(records: list[dict]) -> dict:
        """``{item_code: [name, ...]}`` for codes produced by >1 distinct Tally
        name. Order preserved so the first occurrence is treated as the one kept."""
        by_code: dict = {}
        for r in records:
            name = r.get("_name")
            if not name:
                continue
            by_code.setdefault(safe_item_code(name), [])
            if name not in by_code[safe_item_code(name)]:
                by_code[safe_item_code(name)].append(name)
        return {code: names for code, names in by_code.items() if len(names) > 1}

    def build_doc(self, record: dict) -> dict:
        tally_uom = (record.get("BaseUnits") or "").strip()
        # User-supplied overrides (from pre-flight check) take precedence
        uom = self._uom_overrides.get(tally_uom) or UOM_MAP.get(tally_uom, DEFAULT_UOM)
        doc = {
            "doctype": "Item",
            "item_code": safe_item_code(record["_name"]),
            "item_name": record["_name"],
            "item_group": record.get("Parent") or DEFAULT_ITEM_GROUP,
            "stock_uom": uom,
            "description": record.get("Description") or record["_name"],
            "is_stock_item": self._is_stock_item(record),
            "standard_rate": self._to_float(record.get("StandardPrice")),
            "valuation_rate": self._to_float(record.get("StandardCost")),
            "gst_hsn_code": record.get("HSNCode") or "",
        }
        vm = self._valuation_method(record)
        if vm:
            doc["valuation_method"] = vm
        doc.update(self._gst_fields(record))
        return doc

    def recover_insert(self, data: dict, exc: Exception):
        """India Compliance makes ``gst_hsn_code`` a Link to "GST HSN Code" and
        rejects an Item whose code is not a known HSN - a check that fires even with
        the validate-HSN setting off. Retry once with the HSN cleared so the item
        still lands; the user adds a valid HSN later. Matched on the error text
        because the failure surfaces as a LinkValidationError / India-Compliance
        message, both of which name HSN. A blank HSN can't be salvaged this way
        (nothing to clear), so we only retry when one was actually set."""
        hsn = (data.get("gst_hsn_code") or "").strip()
        if not hsn or "hsn" not in str(exc).lower():
            return None
        retry = dict(data)
        retry["gst_hsn_code"] = ""
        return retry, (
            f"imported without HSN code - ERPNext rejected '{hsn}' (not a valid GST "
            "HSN Code). Add a correct HSN before raising GST invoices.")

    @staticmethod
    def _valuation_method(record: dict):
        """Map Tally's costing/valuation method to ERPNext's Item.valuation_method.

        Tally "Avg. Cost"/"Avg. Price" → "Moving Average"; FIFO/LIFO map straight
        across. Returns None for anything else, so the item keeps the ERPNext
        default (inherited from Stock Settings) rather than getting an invalid value.
        """
        raw = (record.get("ValuationMethod") or "").strip().lower()
        if not raw:
            return None
        if raw.startswith("fifo") or "first in" in raw:
            return "FIFO"
        if raw.startswith("lifo") or "last in" in raw:
            return "LIFO"
        if "avg" in raw or "average" in raw or "moving" in raw:
            return "Moving Average"
        return None

    @staticmethod
    def _is_stock_item(record: dict) -> int:
        """A Tally Stock Item whose GST supply type is 'Services' maps to a
        non-stock Item in ERPNext (read from GSTDETAILS.LIST/SUPPLYTYPE); every
        other supply type stays a stock item."""
        supply = (record.get("TypeOfSupply") or "").strip().lower()
        return 0 if supply in ("services", "service") else 1

    @staticmethod
    def _gst_treatment(gst_type: str):
        """Map a Tally GST type to ERPNext flags, or None when unrecognised.

        Returns a dict of India-Compliance Item flags to set. Empty dict = taxable
        (the default); None = we don't know this value (caller warns)."""
        key = (gst_type or "").strip().lower().replace("-", " ").replace("_", " ")
        key = " ".join(key.split())
        table = {
            "": {},
            "taxable": {},
            "applicable": {},
            "nil rated": {"is_nil_exempt": 1},
            "nil": {"is_nil_exempt": 1},
            "exempt": {"is_nil_exempt": 1},
            "exempted": {"is_nil_exempt": 1},
            "non gst": {"is_non_gst": 1},
            "not applicable": {"is_non_gst": 1},
        }
        return table.get(key)

    def _gst_fields(self, record: dict) -> dict:
        """Item-level GST attributes derived from Tally's GST taxability
        (GSTDETAILS.LIST/TAXABILITY: Taxable / Nil Rated / Exempt / Non-GST).

        ``is_nil_exempt`` / ``is_non_gst`` are India-Compliance fields; setting them
        on the doc is harmless when that app isn't installed (Frappe ignores keys
        that aren't real docfields). Unrecognised values fall back to taxable and
        are flagged as a warning in ``before_run``."""
        # A flat "GST Applicable = Not Applicable" overrides taxability → non-GST.
        if (record.get("GstApplicable") or "").strip().lower() in ("not applicable", "no"):
            return {"is_non_gst": 1}
        return self._gst_treatment(record.get("GSTTaxability") or "") or {}

    def _ensure_item_groups(self, groups: set[str], result: ImportResult) -> None:
        """Create any missing Item Groups under the default parent group.

        A failure here means items in that group will fall back to the default
        group, so it's recorded as a warning (visible loss of grouping) rather
        than failing silently."""
        for group in groups:
            if group and not frappe.db.exists("Item Group", group):
                try:
                    ig = frappe.new_doc("Item Group")
                    ig.item_group_name = group
                    ig.parent_item_group = DEFAULT_ITEM_GROUP
                    ig.insert(ignore_permissions=True)
                    frappe.db.commit()
                except Exception as exc:
                    frappe.log_error("Tally Migrator", f"Item Group creation failed: {group}: {exc}")
                    result.add_warning(group, f"item group not created: {exc}")


# ── Warehouse importer ──────────────────────────────────────────────────────────

class WarehouseImporter(BaseImporter):
    doctype = "Warehouse"
    key_field = "warehouse_name"
    scope_field = "company"   # warehouse_name is unique per company, not globally

    def iter_records(self, records: list[dict]) -> list[dict]:
        return self._topo_sort(records)

    def before_run(self, records: list[dict], result: ImportResult) -> None:
        # A Tally Godown that is the Parent of another Godown must be created as a
        # GROUP warehouse, or the migrated tree is malformed: ERPNext only renders
        # nesting under an is_group warehouse, so children of a leaf parent are
        # orphaned in the warehouse tree (the insert itself does not fail, so the
        # loss is silent). Mirrors how Stock Groups and Cost Centres mark a node
        # that is someone's parent. A parent referenced but absent from the file
        # falls through to _resolve_parent's root fallback, so it needs no flag here.
        self._group_names = {
            (r.get("Parent") or "").strip()
            for r in records if (r.get("Parent") or "").strip()
        }

    def build_doc(self, record: dict) -> dict:
        doc = {
            "doctype": "Warehouse",
            "warehouse_name": record["_name"],
            "company": self.company,
            "address_line_1": record.get("Address") or "",
        }
        if record["_name"] in getattr(self, "_group_names", set()):
            doc["is_group"] = 1
        parent_wh = self._resolve_parent(record.get("Parent", "").strip())
        if parent_wh:
            doc["parent_warehouse"] = parent_wh
        return doc

    def _resolve_parent(self, parent: str) -> str:
        """
        Resolve the ERPNext parent warehouse. Warehouse names are suffixed with
        the company abbreviation. Prefer the migrated Tally parent; otherwise nest
        under the company's root warehouse; otherwise leave top-level.
        """
        if parent:
            candidate = f"{parent} - {self.abbr}"
            if frappe.db.exists("Warehouse", candidate):
                return candidate
        root = f"{DEFAULT_WAREHOUSE} - {self.abbr}"
        return root if frappe.db.exists("Warehouse", root) else ""

    @staticmethod
    def _topo_sort(warehouses: list[dict]) -> list[dict]:
        """Order warehouses so each parent precedes its children (arbitrary depth)."""
        name_set = {w["_name"] for w in warehouses}
        index = {w["_name"]: w for w in warehouses}
        ordered: list[dict] = []
        visited: set[str] = set()
        visiting: set[str] = set()   # nodes on the current DFS path → cycle guard

        def visit(name: str) -> None:
            if name in visited or name not in index:
                return
            visiting.add(name)
            parent = index[name].get("Parent", "").strip()
            if parent and parent in name_set and parent not in visiting:
                visit(parent)
            visiting.discard(name)
            visited.add(name)
            ordered.append(index[name])

        for w in warehouses:
            visit(w["_name"])
        return ordered


# ── Stock Group importer (Tally Stock Groups → nested Item Groups) ────────────

class StockGroupImporter:
    """Recreate Tally's Stock Group tree as ERPNext Item Groups.

    Without this, item groups are created flat from each item's ``Parent`` (see
    ``ItemImporter._ensure_item_groups``), losing Tally's hierarchy. Importing the
    Stock Group masters first gives items a real nested group to nest under; the
    flat fallback then only fires for groups Tally didn't export as masters.

    Item Groups are not company-scoped, so names are used verbatim. Standalone
    (not a BaseImporter) because of parent resolution + parent-before-child order.
    """

    doctype = "Item Group"

    def __init__(self, company: str, abbr: str):
        self.company = company
        self.abbr = abbr

    def run(self, groups: list[dict]) -> ImportResult:
        result = ImportResult(self.doctype)
        names = {g["_name"] for g in groups}
        for node in self._ordered(groups):
            parent = node.get("Parent", "").strip()
            parent_group = parent if parent in names else DEFAULT_ITEM_GROUP
            self._upsert(result, node["_name"], parent_group)
        return result

    @staticmethod
    def _ordered(groups: list[dict]) -> list[dict]:
        """Parent-before-child (arbitrary depth)."""
        index = {g["_name"]: g for g in groups}
        ordered, visited, visiting = [], set(), set()   # visiting = cycle guard

        def visit(name: str) -> None:
            if name in visited or name not in index:
                return
            visiting.add(name)
            parent = index[name].get("Parent", "").strip()
            if parent in index and parent not in visiting:
                visit(parent)
            visiting.discard(name)
            visited.add(name)
            ordered.append(index[name])

        for g in groups:
            visit(g["_name"])
        return ordered

    def _upsert(self, result: ImportResult, name: str, parent_group: str) -> None:
        try:
            if frappe.db.exists("Item Group", name):
                result.skipped += 1
                return
            doc = frappe.get_doc({
                "doctype": "Item Group",
                "item_group_name": name,
                "parent_item_group": parent_group,
                "is_group": 1,
            })
            doc.insert(ignore_permissions=True)
            frappe.db.commit()
            result.add_created(doc.name)
        except Exception as exc:
            result.add_error(name, exc)
            frappe.db.rollback()


# ── Unit importer (Tally Units → ERPNext UOM + conversion factors) ────────────

class UnitImporter:
    """Create ERPNext UOMs (and compound conversions) from Tally Unit masters.

    Today UOMs are only resolved by name when an item is imported; the Unit
    masters themselves (formal name, decimal places, compound relations) are never
    read. This imports them so a UOM carries Tally's formal name + whole-number
    flag, and compound units (e.g. 1 Doz = 12 Nos) become a UOM Conversion Factor.

    Conversion-factor creation is best-effort (it depends on the constituent UOMs
    and a UOM Category): any failure is a non-fatal warning, never a hard error.
    """

    doctype = "UOM"

    def __init__(self, company: str, abbr: str):
        self.company = company
        self.abbr = abbr

    def run(self, units: list[dict]) -> ImportResult:
        result = ImportResult(self.doctype)
        for u in units:
            self._ensure_uom(result, u)
        # Compound conversions need both constituent UOMs to exist first, so do
        # them in a second pass after every simple UOM is created.
        for u in units:
            if not self._is_simple(u):
                self._ensure_conversion(result, u)
        return result

    @staticmethod
    def _is_simple(u: dict) -> bool:
        return (u.get("IsSimpleUnit") or "").strip().lower() not in ("no", "false", "0")

    def _ensure_uom(self, result: ImportResult, u: dict) -> None:
        name = u["_name"]
        try:
            if frappe.db.exists("UOM", name):
                result.skipped += 1
                return
            decimals = (u.get("DecimalPlaces") or "").strip()
            doc = frappe.get_doc({
                "doctype": "UOM",
                "uom_name": name,
                # Tally decimalplaces=0 → quantity must be whole.
                "must_be_whole_number": 1 if decimals in ("", "0") else 0,
            })
            doc.insert(ignore_permissions=True)
            frappe.db.commit()
            result.add_created(doc.name)
        except Exception as exc:
            result.add_error(name, exc)
            frappe.db.rollback()

    def _ensure_conversion(self, result: ImportResult, u: dict) -> None:
        """Compound unit '1 BaseUnits = Conversion AdditionalUnits' → UOM
        Conversion Factor. Non-fatal: warn if it can't be created."""
        base = (u.get("BaseUnits") or "").strip()
        additional = (u.get("AdditionalUnits") or "").strip()
        factor = BaseImporter._to_float(u.get("Conversion"))
        if not (base and additional and factor > 0):
            return
        try:
            exists = frappe.db.exists(
                "UOM Conversion Factor", {"from_uom": base, "to_uom": additional})
            if exists:
                return
            doc = {
                "doctype": "UOM Conversion Factor",
                "from_uom": base,
                "to_uom": additional,
                "value": factor,
            }
            category = self._uom_category(result)
            if category:
                doc["category"] = category
            frappe.get_doc(doc).insert(ignore_permissions=True)
            frappe.db.commit()
        except Exception as exc:
            frappe.log_error("Tally Migrator", f"UOM conversion failed for {u['_name']}: {exc}")
            result.add_warning(
                u["_name"],
                f"compound unit conversion (1 {base} = {factor:g} {additional}) "
                f"not created: {exc}")
            frappe.db.rollback()

    @staticmethod
    def _uom_category(result: "ImportResult | None" = None) -> str:
        """A UOM Category to attach conversions to (required in recent ERPNext).
        Reuse one if present, else create a 'Tally Imported' category.

        Creating the category is a side effect the user didn't explicitly ask for,
        so when we do create one we record a one-off warning (the category is reused
        thereafter, so this fires at most once per run) - the auto-created master is
        then visible/auditable in the log rather than appearing silently."""
        if not frappe.db.has_column("UOM Conversion Factor", "category"):
            return ""
        existing = frappe.get_all("UOM Category", pluck="name", limit=1)
        if existing:
            return existing[0]
        try:
            cat = frappe.get_doc({"doctype": "UOM Category", "category_name": "Tally Imported"})
            cat.insert(ignore_permissions=True)
            frappe.db.commit()
            if result is not None:
                result.add_warning(
                    "UOM Category",
                    "auto-created a 'Tally Imported' UOM Category to hold compound-unit "
                    "conversions - ERPNext requires every conversion to belong to a "
                    "category and none existed.")
            return cat.name
        except Exception:
            frappe.db.rollback()
            return ""


# ── Chart of Accounts importer ───────────────────────────────────────────────

class AccountImporter:
    """Creates the Chart of Accounts (groups + ledger accounts).

    Two modes:
    - ``reuse``  (default): Tally's reserved groups are NOT recreated; their
      ERPNext standard-COA equivalents are used as parents. Only custom groups
      and ledger accounts are created.
    - ``mirror`` : every Tally group is recreated verbatim.

    Parties (ledgers under Sundry Debtors/Creditors) are excluded upstream by the
    extractor - they are Customers/Suppliers, not ledger Accounts.

    Standalone (not a BaseImporter) because parent resolution + topological group
    ordering don't fit the simple key_field upsert template.
    """

    doctype = "Account"

    def __init__(self, company: str, abbr: str, mode: str = "reuse"):
        self.company = company
        self.abbr = abbr
        self.mode = mode if mode in ("reuse", "mirror") else "reuse"
        self._group_cache: dict[str, str] = {}

    def run(self, accounts: list) -> ImportResult:
        result = ImportResult(self.doctype)
        for node in self._ordered(self._select(accounts)):
            parent = self._resolve_parent(node)
            if not parent:
                result.add_error(node.name, "could not resolve a parent account")
                continue
            self._upsert(result, node, parent)
        return result

    # ── Selection + ordering ─────────────────────────────────────────────────
    def _select(self, accounts: list) -> list:
        if self.mode == "mirror":
            return list(accounts)
        # reuse: reserved groups already exist in ERPNext - don't recreate them.
        return [a for a in accounts if not (a.is_group and a.is_reserved)]

    def _ordered(self, nodes: list) -> list:
        groups = [n for n in nodes if n.is_group]
        ledgers = [n for n in nodes if not n.is_group]
        return self._topo_groups(groups) + ledgers

    @staticmethod
    def _topo_groups(groups: list) -> list:
        index = {g.name: g for g in groups}
        ordered, visited, visiting = [], set(), set()   # visiting = cycle guard

        def visit(name: str) -> None:
            if name in visited or name not in index:
                return
            visiting.add(name)
            parent = index[name].parent
            if parent in index and parent not in visiting:
                visit(parent)
            visiting.discard(name)
            visited.add(name)
            ordered.append(index[name])

        for g in groups:
            visit(g.name)
        return ordered

    # ── Parent resolution ────────────────────────────────────────────────────
    def _erp_name(self, base: str) -> str:
        return company_scoped(base, self.abbr)

    def _resolve_parent(self, node) -> str | None:
        parent = node.parent
        if self.mode == "mirror":
            return self._erp_name(parent) if parent else self._root_group(node.root_type)
        if not parent:
            return self._root_group(node.root_type)
        cls = classify_group(parent)
        if cls:  # parent is a reserved group → use its ERPNext default group
            return self._default_group(cls["erpnext_group"], node.root_type)
        return self._erp_name(parent)  # custom parent was (or will be) recreated

    def _default_group(self, base: str, root_type: str) -> str | None:
        if base in self._group_cache:
            return self._group_cache[base]
        candidate = self._erp_name(base)
        resolved = candidate if frappe.db.exists("Account", candidate) else self._root_group(root_type)
        if resolved:
            self._group_cache[base] = resolved
        return resolved

    def _root_group(self, root_type: str) -> str | None:
        base = ERPNEXT_ROOT_GROUPS.get(root_type)
        if base:
            candidate = self._erp_name(base)
            if frappe.db.exists("Account", candidate):
                return candidate
        rows = frappe.get_all(
            "Account", fields=["name", "parent_account"],
            filters={"root_type": root_type, "is_group": 1, "company": self.company},
        )
        for r in rows:
            if not r.get("parent_account"):
                return r["name"]
        return rows[0]["name"] if rows else None

    def _upsert(self, result: ImportResult, node, parent: str) -> None:
        try:
            if frappe.db.exists("Account", self._erp_name(node.name)):
                result.skipped += 1
                return
            doc = {
                "doctype": "Account",
                "account_name": node.name,
                "company": self.company,
                "parent_account": parent,
                "is_group": 1 if node.is_group else 0,
                "root_type": node.root_type,
            }
            if node.account_type:
                doc["account_type"] = node.account_type
            d = frappe.get_doc(doc)
            d.insert(ignore_permissions=True)
            frappe.db.commit()
            result.add_created(d.name)
        except Exception as exc:
            result.add_error(node.name, exc)
            frappe.db.rollback()
            return
        # A Tally bank ledger carries the company's own account no + IFSC → create a
        # company Bank Account linked to this GL account. Separate, non-fatal step so
        # a Bank-Account quirk can't roll back the account that was just created.
        if not node.is_group and node.account_type == "Bank" and node.bank_account_no:
            self._save_company_bank_account(node, d.name, result)

    def _save_company_bank_account(self, node, account_name: str,
                                   result: ImportResult) -> None:
        bank = _ensure_bank(node.bank_name)
        if not bank:
            result.add_warning(
                node.name, "bank account not created: no bank name on the ledger")
            return
        _insert_bank_account(
            account_name=node.bank_holder or node.name,
            bank=bank,
            account_no=node.bank_account_no,
            ifsc=node.bank_ifsc,
            gl_account=account_name,        # link to the GL account just created
            is_company=True,
            result=result,
            warn_name=node.name,
            count_created=True,
        )


# ── Cost Centre importer ─────────────────────────────────────────────────────

class CostCentreImporter:
    """Creates Cost Centers (flat or nested) under the company's root centre."""

    doctype = "Cost Center"

    def __init__(self, company: str, abbr: str):
        self.company = company
        self.abbr = abbr

    def run(self, centres: list) -> ImportResult:
        result = ImportResult(self.doctype)
        names = {c.name for c in centres}
        parents = {c.parent for c in centres if c.parent}
        root = self._root_centre()
        if not root:
            for c in centres:
                result.add_error(c.name, "no root cost center found in ERPNext")
            return result
        for node in self._ordered(centres):
            parent = self._erp_name(node.parent) if node.parent in names else root
            self._upsert(result, node, node.name in parents, parent)
        return result

    def _erp_name(self, base: str) -> str:
        return company_scoped(base, self.abbr)

    @staticmethod
    def _ordered(centres: list) -> list:
        index = {c.name: c for c in centres}
        ordered, visited, visiting = [], set(), set()   # visiting = cycle guard

        def visit(name: str) -> None:
            if name in visited or name not in index:
                return
            visiting.add(name)
            parent = index[name].parent
            if parent in index and parent not in visiting:
                visit(parent)
            visiting.discard(name)
            visited.add(name)
            ordered.append(index[name])

        for c in centres:
            visit(c.name)
        return ordered

    def _root_centre(self) -> str | None:
        rows = frappe.get_all(
            "Cost Center", fields=["name", "parent_cost_center"],
            filters={"company": self.company, "is_group": 1},
        )
        for r in rows:
            if not r.get("parent_cost_center"):
                return r["name"]
        return rows[0]["name"] if rows else None

    def _upsert(self, result: ImportResult, node, is_group: bool, parent: str) -> None:
        try:
            if frappe.db.exists("Cost Center", self._erp_name(node.name)):
                result.skipped += 1
                return
            d = frappe.get_doc({
                "doctype": "Cost Center",
                "cost_center_name": node.name,
                "parent_cost_center": parent,
                "company": self.company,
                "is_group": 1 if is_group else 0,
            })
            d.insert(ignore_permissions=True)
            frappe.db.commit()
            result.add_created(d.name)
        except Exception as exc:
            result.add_error(node.name, exc)
            frappe.db.rollback()


# ── Concurrency guard for opening entries ─────────────────────────────────────
# The opening Journal Entry and Stock Reconciliation are aggregate, submitted
# documents guarded against re-posting by an existence check (see
# OpeningBalanceImporter._existing_opening_state). That check is read-then-write,
# so two runs of the SAME company racing each other could both read "none exist"
# and both post - doubling the opening trial balance. A short, self-expiring Redis
# lock per company serialises the check-and-post: only one run posts at a time, and
# the other then sees the now-committed entries (via the existence check) and stands
# down. Lock + existence check together close the window with no stale-lock risk.
_OPENING_LOCK_TTL = 3600   # seconds; ample to post, self-expiring if a worker dies


@contextlib.contextmanager
def _company_opening_lock(company: str):
    """Best-effort per-company lock around opening-balance/stock posting.

    Yields True when this process holds the lock, False when another run already
    does. Site-namespaced so it is safe on shared Redis. If the cache is
    unavailable the lock is treated as acquired - the DB-level existence check
    remains the correctness backstop, so a cache outage never blocks a real run.
    """
    site = getattr(frappe.local, "site", "") or ""
    key = f"tally_migrator:opening:{site}:{company}"
    cache = frappe.cache()
    acquired = False
    try:
        # redis SETNX + TTL: set only if absent, auto-expire so a dead worker can't
        # hold the lock forever.
        acquired = bool(cache.set(key, frappe.utils.now(), nx=True, ex=_OPENING_LOCK_TTL))
    except Exception:
        # Cache down / unavailable: proceed without the lock. The existence check
        # still prevents the common (sequential) double-post.
        yield True
        return
    try:
        yield acquired
    finally:
        if acquired:
            try:
                cache.delete(key)
            except Exception:
                pass


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


# ── Opening balance importer ─────────────────────────────────────────────────

class OpeningBalanceImporter:
    """Posts one balanced 'Opening Entry' Journal Entry for the whole trial balance.

    Three balance sources are combined into a single submitted JE:
      • ledger accounts  - Dr/Cr against the account itself,
      • customers        - against the company's default Receivable account, with
                           ``party_type='Customer'`` / ``party=<name>``,
      • suppliers        - against the default Payable account, ``party=<name>``.

    Referenced accounts/parties are normally created first (COA + Customers +
    Suppliers). A line whose account/party did *not* get created (its earlier import
    failed) is skipped with a warning rather than included - otherwise a single
    missing reference would make the whole submitted entry throw and roll back,
    silently dropping *every* opening balance. ERPNext requires the JE to balance;
    any residual difference
    (e.g. only part of the trial balance was migrated) is absorbed by a balancing
    line against 'Temporary Opening - <ABBR>'. The entry is **submitted** so the
    balances actually post to the General Ledger.
    """

    # Marker written to every batch's ``user_remark`` so a later re-run can tell
    # which batches this migrator already posted (per-batch idempotency) and skip
    # only those, posting the rest. Kept in one place so the writer (``_post_batch``)
    # and reader (``_existing_opening_state``) can never drift apart.
    _REMARK_PREFIX = "Opening balances imported from Tally ("

    def __init__(self, company: str, abbr: str):
        self.company = company
        self.abbr = abbr

    def run(self, accounts: list, customers: list, suppliers: list,
            posting_date: str) -> ImportResult:
        result = ImportResult("Journal Entry")
        # Idempotency: the opening JE is an aggregate document, not a per-record
        # upsert, so re-posting a batch would double the books. But posting is
        # per-batch (one JE per root type / party type), so the guard must be
        # per-batch too - otherwise a re-run after a *partial* failure (some
        # batches committed, one failed) would see the committed batches and skip
        # *everything*, silently abandoning the balances that never posted.
        #
        # ``foreign`` is True when an Opening Entry exists that this migrator did
        # NOT create (e.g. the company's books were opened manually); in that case
        # we stay conservative and skip entirely rather than risk double-posting
        # against a hand-built opening trial balance.
        foreign, posted = self._existing_opening_state()
        if foreign:
            result.skipped += 1
            result.add_warning(
                "Opening Entry",
                "this company already has an Opening Entry that was not created by "
                "this migrator - opening balances were skipped to avoid double-posting "
                "against a manually set-up book. Cancel that entry first if you want "
                "this migration to post the opening balances instead.")
            return result

        # Build the entry in batches (one per root type, plus Customers and
        # Suppliers) rather than a single all-or-nothing document. A validation
        # failure on submit then loses only its own batch, not the whole trial
        # balance, and each batch commits independently. Every batch is balanced
        # against 'Temporary Opening', so the batches' plugs net to exactly the
        # same Temporary Opening balance a single combined entry would produce.
        batches: list[tuple[str, list[dict]]] = []
        for root_type in ("Asset", "Liability", "Equity", "Income", "Expense"):
            rows = self._account_lines(
                [a for a in accounts if a.root_type == root_type], result)
            if rows:
                batches.append((root_type, rows))
        cust = self._party_lines(customers, "Customer", "default_receivable_account", result)
        if cust:
            batches.append(("Customer", cust))
        supp = self._party_lines(suppliers, "Supplier", "default_payable_account", result)
        if supp:
            batches.append(("Supplier", supp))
        if not batches:
            return result  # nothing to post

        # Drop batches a prior run already posted (per-batch idempotency); post the
        # rest. This is what makes "fix it and re-run" actually complete a partially
        # posted opening trial balance instead of skipping it wholesale.
        pending: list[tuple[str, list[dict]]] = []
        for label, lines in batches:
            if label in posted:
                result.skipped += 1
                result.add_warning(
                    label,
                    f"opening balances for {label} were already posted in an earlier "
                    "run - skipped to avoid double-posting. The remaining batches (if "
                    "any) were posted now.")
                continue
            pending.append((label, lines))
        if not pending:
            return result  # every batch already posted

        # The 'did not net to zero' check is meaningful only across the balances we
        # are actually posting now - per-batch plugs are expected and large - so warn
        # once here over the pending batches, then let each batch plug silently.
        self._warn_residual([l for _, rows in pending for l in rows], result)
        for label, lines in pending:
            self._post_batch(label, lines, posting_date, result)
        return result

    def _post_batch(self, label: str, lines: list[dict], posting_date: str,
                    result: ImportResult) -> None:
        """Insert + submit one Opening Entry batch, committing on its own.

        Plugs the batch against Temporary Opening (silently - the aggregate
        residual is warned once by the caller). A failure rolls back only this
        batch's transaction; batches already committed are unaffected."""
        self._balance(lines)
        try:
            doc = frappe.get_doc({
                "doctype": "Journal Entry",
                "voucher_type": "Opening Entry",
                "posting_date": posting_date,
                "company": self.company,
                "accounts": lines,
                "user_remark": f"{self._REMARK_PREFIX}{label})",
            })
            doc.insert(ignore_permissions=True)
            doc.submit()
            frappe.db.commit()
            result.add_created(doc.name)
        except Exception as exc:
            result.add_error(f"Opening Entry ({label})", exc)
            frappe.db.rollback()

    def _existing_opening_state(self) -> tuple[bool, set[str]]:
        """Inspect this company's non-cancelled Opening Entries.

        Returns ``(foreign, posted)``:
        - ``posted`` - the set of batch labels this migrator already posted,
          recovered from each entry's ``user_remark`` marker, so a re-run can skip
          exactly those batches and post the rest.
        - ``foreign`` - True when an Opening Entry exists whose remark this migrator
          did *not* write (opening balances set up by hand or another tool); the
          caller then skips entirely rather than double-post against those books.
        """
        remarks = frappe.get_all(
            "Journal Entry",
            filters={
                "company": self.company,
                "voucher_type": "Opening Entry",
                "docstatus": ["<", 2],   # draft or submitted, not cancelled
            },
            pluck="user_remark",
        )
        posted: set[str] = set()
        foreign = False
        for remark in remarks or []:
            label = self._batch_label_from_remark(remark)
            if label:
                posted.add(label)
            else:
                foreign = True
        return foreign, posted

    @classmethod
    def _batch_label_from_remark(cls, remark) -> str:
        """Recover the batch label from a ``user_remark`` we wrote, else "".

        ``"Opening balances imported from Tally (Asset)"`` → ``"Asset"``; anything
        that doesn't match our marker (a hand-written or third-party entry) → "".
        """
        s = (remark or "").strip()
        if s.startswith(cls._REMARK_PREFIX) and s.endswith(")"):
            return s[len(cls._REMARK_PREFIX):-1].strip()
        return ""

    # ── Line builders ────────────────────────────────────────────────────────
    def _account_lines(self, accounts: list, result: ImportResult) -> list[dict]:
        lines = []
        for node in accounts:
            if not node.opening_balance or node.is_group:
                continue
            account = company_scoped(node.name, self.abbr)
            if not frappe.db.exists("Account", account):
                result.add_warning(
                    node.name,
                    f"opening balance skipped - account '{account}' was not created "
                    "(its import failed earlier). Fix the account and re-run.")
                continue
            line = self._line(account, node.opening_balance, node.opening_dr_cr)
            # ERPNext requires a cost center on every journal line that posts to a
            # P&L (Income/Expense) account - without it the whole Opening Entry batch
            # fails on submit. Balance-sheet lines need none. P&L openings only occur
            # in a mid-year migration; attach the company default so the figure posts
            # rather than dropping it. Skip with a warning if no default is set.
            if node.root_type in ("Income", "Expense"):
                cost_center = self._default_cost_center()
                if not cost_center:
                    result.add_warning(
                        node.name,
                        "opening balance skipped - it posts to a P&L account, which "
                        "needs a cost center, but the company has no default cost "
                        "center set. Set it on the Company and re-run.")
                    continue
                line["cost_center"] = cost_center
            lines.append(line)
        return lines

    def _default_cost_center(self) -> str:
        """The company's default cost center (required on P&L opening lines).
        Resolved once per run."""
        if not hasattr(self, "_cost_center"):
            self._cost_center = frappe.get_cached_value(
                "Company", self.company, "cost_center") or ""
        return self._cost_center

    # Tally party name → the field ERPNext stores it under; the document's own
    # ``name`` can differ (e.g. a naming series), so we resolve it rather than
    # assuming ``name == display name``.
    _PARTY_KEY_FIELD = {"Customer": "customer_name", "Supplier": "supplier_name"}

    def _party_lines(self, parties: list, party_type: str, company_field: str,
                     result: ImportResult) -> list[dict]:
        if not parties:
            return []
        control = frappe.get_cached_value("Company", self.company, company_field)
        key_field = self._PARTY_KEY_FIELD[party_type]
        lines, missing_control = [], False
        for record in parties:
            amount, drcr = TallyExtractor._parse_opening(record.get("OpeningBalance", ""))
            if not amount:
                continue
            if not control:
                missing_control = True
                continue
            # Resolve the actual document name (handles naming series and any
            # existing party matched on display name but stored under another id).
            # A missing name means the party wasn't created - skip with a warning
            # rather than posting against, or failing the whole entry on, a bad id.
            party_name = frappe.db.get_value(party_type, {key_field: record["_name"]}, "name")
            if not party_name:
                result.add_warning(
                    record["_name"],
                    f"opening balance skipped - {party_type} '{record['_name']}' was "
                    "not created (its import failed earlier). Fix it and re-run.")
                continue
            line = self._line(control, amount, drcr)
            line.update({"party_type": party_type, "party": party_name})
            lines.append(line)
        if missing_control:
            result.add_error(
                f"{party_type} opening balances",
                f"company has no {company_field.replace('_', ' ')} set - skipped",
            )
        return lines

    @staticmethod
    def _line(account: str, amount: float, drcr: str) -> dict:
        """A JE line; Dr (or blank) → debit, Cr → credit."""
        if drcr == "Cr":
            return {"account": account, "debit_in_account_currency": 0.0,
                    "credit_in_account_currency": amount}
        return {"account": account, "debit_in_account_currency": amount,
                "credit_in_account_currency": 0.0}

    # A residual below this (currency units) is rounding noise, not a real gap.
    PLUG_NOISE_THRESHOLD = 1.0

    def _balance(self, lines: list[dict], result: "ImportResult | None" = None) -> None:
        # A non-trivial residual means the migrated balances don't net to zero on
        # their own (usually only part of the trial balance was migrated). When a
        # result is given, surface that gap before plugging it; the batch path warns
        # once on the aggregate instead and calls this with no result.
        if result is not None:
            self._warn_residual(lines, result)
        total_dr = sum(l["debit_in_account_currency"] for l in lines)
        total_cr = sum(l["credit_in_account_currency"] for l in lines)
        diff = round(total_dr - total_cr, 2)
        if diff == 0:
            return
        temp = company_scoped("Temporary Opening", self.abbr)
        if diff > 0:
            lines.append({"account": temp, "debit_in_account_currency": 0.0,
                          "credit_in_account_currency": diff})
        else:
            lines.append({"account": temp, "debit_in_account_currency": abs(diff),
                          "credit_in_account_currency": 0.0})

    def _warn_residual(self, lines: list[dict], result: ImportResult) -> None:
        """Flag a non-fatal warning when the lines don't net to zero (a Temporary
        Opening plug will absorb the gap, so it must not be hidden)."""
        diff = round(sum(l["debit_in_account_currency"] for l in lines)
                     - sum(l["credit_in_account_currency"] for l in lines), 2)
        if abs(diff) < self.PLUG_NOISE_THRESHOLD:
            return
        side = "credit" if diff > 0 else "debit"
        result.add_warning(
            "Opening Entry",
            f"opening balances did not net to zero; {abs(diff):,.2f} was {side}ed "
            f"to 'Temporary Opening - {self.abbr}' to balance the entry - review "
            "whether the full trial balance was migrated.")


# ── Party opening balances (invoice-wise) ────────────────────────────────────

class PartyOpeningImporter:
    """Posts party (customer/supplier) opening balances *invoice-wise*.

    The lump-sum JE line per party (the old ``OpeningBalanceImporter`` party path)
    is trial-balance correct but destroys bill-level traceability: every future
    payment reconciles against a single lump, so aged debtors/creditors and
    invoice-by-invoice matching are lost. This importer instead reproduces each
    outstanding bill Tally carries in ``BILLALLOCATIONS.LIST`` (extracted as
    :class:`~tally_migrator.tally.extractors.BillAllocation`) as a real ERPNext
    opening document:

    - **Outstanding bill** (on the party's natural side) -> a one-line opening
      Sales/Purchase Invoice (``is_opening='Yes'``), contra'd to Temporary Opening
      exactly like ERPNext's own Opening Invoice Creation Tool. Future Payment
      Entries then reconcile against it individually.
    - **Advance / credit balance** (Tally's ISADVANCE flag, or a bill on the side
      opposite the party's natural side) -> an opening Payment Entry, left
      unallocated so it is ready to apply against a later invoice.
    - **No bill detail** (party opening with an empty BILLALLOCATIONS list) -> a
      single lump opening invoice for the whole ledger opening (chosen fallback),
      so the party is still reconcilable as one outstanding rather than a JE line.
    - **Bills do not reconcile to the ledger opening** -> the bills post as-is and
      the residual (ledger opening minus bills) posts as one "On Account" plug on
      the natural side, with a warning, so the party's net still ties to the
      ledger figure (and thus the trial balance).

    Non-party balances (cash, assets, P&L) stay with ``OpeningBalanceImporter``;
    the two compose in the orchestrator. Idempotency: every document carries a
    marker in ``remarks`` (``party | bill``); a re-run reads the markers already
    posted and skips exactly those, so "fix and re-run" fills gaps without
    double-posting.

    The Tally bill id (BILLALLOCATIONS.LIST/NAME) names the ERPNext opening invoice
    (``insert(set_name=...)``), exactly like ERPNext's Opening Invoice Creation Tool's
    "invoice number from the previous system", so the document id reconciles directly
    against the Tally invoice. For Purchase it is also kept in ``bill_no`` (Supplier
    Invoice No). A bill id that collides with an existing document falls back to
    auto-naming with a warning, so an opening is never lost to an id clash.

    Posting date is the migration opening date (not the original bill date): an
    opening invoice backdated into a year with no Fiscal Year record would be
    rejected, so the original bill date is preserved in ``bill_date`` (Purchase) and
    the remarks marker. Single-currency only (v1); a foreign-currency party is
    handled by the ledger-level fallback upstream.
    """

    _MARKER = "Tally opening"          # remarks prefix → idempotency key
    _PLUG_THRESHOLD = 1.0              # a residual below this is rounding noise
    _PARTY_KEY_FIELD = {"Customer": "customer_name", "Supplier": "supplier_name"}

    def __init__(self, company: str, abbr: str, posting_date: str):
        self.company = company
        self.abbr = abbr
        self.posting_date = posting_date
        # Set for real in run() from the parties' ledger currencies; default blank so
        # _process()/_tally_currency_foreign() stay valid (and treat the book as
        # single-currency) even if _process is exercised before run() populates it.
        self._tally_base_ccy = ""

    # ── Orchestration ─────────────────────────────────────────────────────────
    def run(self, bills: list, customers: list[dict],
            suppliers: list[dict]) -> ImportResult:
        result = ImportResult("Opening Invoice")
        # The Tally base currency, derived as the most common ledger CurrencyName
        # across all parties (a forex party carries a different one). Used to skip a
        # party whose ledger currency differs from the base - the in-file signal the
        # ERPNext default_currency guard can't give us, since freshly imported parties
        # carry no currency yet. Equality only, never an ISO-code mapping.
        self._tally_base_ccy = self._base_currency(customers, suppliers)
        by_party: dict[str, list] = {}
        for b in bills or []:
            by_party.setdefault(b.party, []).append(b)
        seen = self._existing_markers()
        self._process(customers, "Customer", by_party, seen, result)
        self._process(suppliers, "Supplier", by_party, seen, result)
        return result

    def _process(self, parties: list[dict], party_type: str,
                 by_party: dict, seen: set, result: ImportResult) -> None:
        if not parties:
            return
        key_field = self._PARTY_KEY_FIELD[party_type]
        for record in parties:
            tally_name = record["_name"]
            ledger_amt, ledger_drcr = TallyExtractor._parse_opening(
                record.get("OpeningBalance", ""))
            party_bills = by_party.get(tally_name, [])
            if not ledger_amt and not party_bills:
                continue  # nothing to post for this party
            party = frappe.db.get_value(party_type, {key_field: tally_name}, "name")
            if not party:
                result.add_warning(
                    tally_name,
                    f"opening balance skipped - {party_type} '{tally_name}' was not "
                    "created (its import failed earlier). Fix it and re-run.")
                continue

            # Multi-currency guard (v1): an opening document posted in the company
            # currency for a party that bills in another currency would silently
            # misstate the balance (the exchange rate is unknown). Two independent
            # signals, either of which marks a party foreign: the ledger's own
            # CurrencyName differs from the Tally base currency (read from the file),
            # or the already-created ERPNext party carries a non-company currency. The
            # file signal is what actually fires here, since a freshly imported party
            # has no currency set yet. Skip with a clear warning rather than post a
            # wrong figure - the user enters that party's opening manually.
            if (self._tally_currency_foreign(record)
                    or self._is_foreign_currency_party(party_type, party)):
                result.add_warning(
                    tally_name,
                    f"opening balance skipped - {party_type} '{tally_name}' uses a "
                    "currency other than the company currency, and invoice-wise "
                    "opening balances are single-currency in this version. Enter this "
                    "party's opening invoices/payments manually so the exchange rate "
                    "is correct.")
                continue

            ledger_signed = self._signed(ledger_amt, ledger_drcr)
            bills_signed = 0.0
            for b in party_bills:
                bills_signed += self._signed(b.amount, b.dr_cr)
                # real_bill: this carries a genuine Tally bill reference (not the lump
                # 'Opening'/'On Account' plug below), so the opening invoice is named
                # after it for direct reconciliation against the Tally invoice id.
                self._emit(party_type, party, tally_name, self._signed(b.amount, b.dr_cr),
                           b.bill_no, b.bill_date, b.is_advance, seen, result,
                           real_bill=True)

            residual = round(ledger_signed - bills_signed, 2)
            if abs(residual) >= self._PLUG_THRESHOLD:
                # No bills at all -> this *is* the party's opening (normal lump, no
                # warning). Bills present but short -> a real mismatch (warn).
                if party_bills:
                    result.add_warning(
                        tally_name,
                        f"bill-wise openings for '{tally_name}' did not add up to its "
                        f"ledger opening; {abs(residual):,.2f} was posted as an "
                        "'On Account' opening to match the ledger. Review the party's "
                        "outstanding bills in Tally.")
                label = "On Account" if party_bills else "Opening"
                self._emit(party_type, party, tally_name, residual,
                           label, self.posting_date, False, seen, result)

    # ── Per-bill emission ─────────────────────────────────────────────────────
    def _emit(self, party_type: str, party: str, tally_name: str, signed: float,
              bill_no: str, bill_date: str, is_advance: bool,
              seen: set, result: ImportResult, real_bill: bool = False) -> None:
        amount = abs(signed)
        if amount < self._PLUG_THRESHOLD:
            return
        marker = f"{self._MARKER}: {tally_name} | {bill_no}"
        if marker in seen:
            result.skipped += 1
            return
        kind = self._classify(party_type, signed, is_advance)
        try:
            if kind == "advance":
                data = self._advance_dict(party_type, party, amount, bill_no, marker)
                set_name = None
            else:
                data = self._invoice_dict(
                    party_type, party, amount, bill_no, bill_date, marker)
                # Name the opening invoice after the Tally bill id, exactly like
                # ERPNext's own Opening Invoice Creation Tool (invoice_number ->
                # set_name): the ERPNext document id then IS the previous-system
                # invoice id, so a future payment / report reconciles against the
                # Tally invoice directly. Only for a genuine bill reference, never the
                # 'Opening'/'On Account' lump plug.
                set_name = bill_no if real_bill else None
            doc = self._insert_invoice(data, set_name, tally_name, bill_no, result)
            doc.submit()
            frappe.db.commit()
            seen.add(marker)
            # Real doctype (Sales/Purchase Invoice or Payment Entry), not the
            # "Opening Invoice" label, so the log can deep-link each document.
            result.add_created(doc.name, doc.doctype)
        except Exception as exc:
            frappe.db.rollback()
            result.add_error(f"{tally_name} | {bill_no}", exc)

    def _insert_invoice(self, data: dict, set_name, tally_name: str, bill_no: str,
                        result: ImportResult):
        """Insert the opening document, naming it after the Tally bill id when given.

        A bill id can collide across parties (two ledgers each carrying a 'Bill 1'),
        and ERPNext document ids are global, so a clash would otherwise fail the whole
        opening. On a duplicate we fall back to auto-naming so the opening still posts
        - the Tally id stays recoverable (Purchase 'Supplier Invoice No' + the remarks
        marker) - and flag that it could not be used as the document id."""
        doc = frappe.get_doc(data)
        doc.flags.ignore_mandatory = True
        if not set_name:
            doc.insert(ignore_permissions=True)
            return doc
        try:
            doc.insert(ignore_permissions=True, set_name=set_name)
            return doc
        except frappe.DuplicateEntryError:
            frappe.db.rollback()
            result.add_warning(
                tally_name,
                f"opening invoice could not use the Tally bill id '{bill_no}' as its "
                "ERPNext id (already in use); it was auto-named instead. Reconcile via "
                "the Supplier Invoice No / remarks.")
            doc = frappe.get_doc(data)
            doc.flags.ignore_mandatory = True
            doc.insert(ignore_permissions=True)
            return doc

    @staticmethod
    def _classify(party_type: str, signed: float, is_advance: bool) -> str:
        """An outstanding invoice (party's natural side, not flagged advance) vs an
        advance/credit. Customer natural side = Dr (signed > 0); Supplier = Cr
        (signed < 0). Tally's ISADVANCE flag forces 'advance' regardless of side."""
        if is_advance:
            return "advance"
        on_natural_side = (signed > 0) if party_type == "Customer" else (signed < 0)
        return "invoice" if on_natural_side else "advance"

    @staticmethod
    def _signed(amount: float, dr_cr: str) -> float:
        """Dr-positive signed amount (so a customer receivable is positive)."""
        return amount if dr_cr == "Dr" else -amount

    # ── Document builders (pure given the resolved company context) ────────────
    def _invoice_dict(self, party_type: str, party: str, amount: float,
                      bill_no: str, bill_date: str, marker: str) -> dict:
        is_sales = party_type == "Customer"
        item = {
            "item_name": f"Opening - {bill_no}"[:140],
            "description": f"Opening balance ({bill_no})",
            "uom": self._stock_uom(),
            "conversion_factor": 1.0,
            "qty": 1,
            "rate": amount,
            ("income_account" if is_sales else "expense_account"): self._temp_account(),
            "cost_center": self._cost_center(),
        }
        data = {
            "doctype": "Sales Invoice" if is_sales else "Purchase Invoice",
            "company": self.company,
            "is_opening": "Yes",
            "set_posting_time": 1,
            "posting_date": self.posting_date,
            "due_date": self.posting_date,
            frappe.scrub(party_type): party,
            "items": [item],
            "update_stock": 0,
            "disable_rounded_total": 1,
            "remarks": marker,
        }
        if not is_sales:
            # Purchase Invoice has native supplier-bill fields - preserve the real
            # Tally bill reference and date here (Sales Invoice has none, so the
            # marker in remarks is the only carrier there).
            data["bill_no"] = bill_no
            bd = bill_date or self.posting_date
            data["bill_date"] = bd
            # A Purchase Invoice's due date can never precede its supplier-invoice
            # (bill) date - ERPNext rejects it ("Due Date cannot be before Supplier
            # Invoice Date"). The opening posting_date is normally >= the bill date,
            # but real Tally data can carry a bill dated after the opening date, so
            # clamp the due date up to the bill date when that happens.
            try:
                from frappe.utils import getdate
                if getdate(bd) > getdate(self.posting_date):
                    data["due_date"] = bd
            except Exception:
                pass
        return data

    def _advance_dict(self, party_type: str, party: str, amount: float,
                      bill_no: str, marker: str) -> dict:
        """An opening advance as an unallocated Payment Entry. A customer advance is
        money Received (Debtors -> Temporary Opening); a supplier advance is money
        Paid (Temporary Opening -> Creditors). No references, so the whole amount
        sits as an advance ready to apply against a future invoice."""
        is_customer = party_type == "Customer"
        party_account = self._party_account(party_type)
        temp = self._temp_account()
        currency = self._company_currency()
        data = {
            "doctype": "Payment Entry",
            "payment_type": "Receive" if is_customer else "Pay",
            "company": self.company,
            "posting_date": self.posting_date,
            "party_type": party_type,
            "party": party,
            "paid_amount": amount,
            "received_amount": amount,
            "paid_from": party_account if is_customer else temp,
            "paid_to": temp if is_customer else party_account,
            "paid_from_account_currency": currency,
            "paid_to_account_currency": currency,
            # Payment Entry.set_remarks() OVERWRITES remarks with an auto-generated
            # string on save - which silently wiped our idempotency marker, so a re-run
            # could not detect the advance and re-created it (doubling Debtors/Creditors
            # advances). custom_remarks=1 tells ERPNext to keep our remarks verbatim, so
            # the marker survives for both the re-run guard (_existing_markers) and the
            # reconciliation (_opening_account_balance), exactly like the invoice path.
            "custom_remarks": 1,
            "remarks": marker,
            "reference_no": bill_no,
            "reference_date": self.posting_date,
        }
        return data

    # ── Resolved-once company context ──────────────────────────────────────────
    def _temp_account(self) -> str:
        if not hasattr(self, "_temp"):
            self._temp = company_scoped("Temporary Opening", self.abbr)
        return self._temp

    def _cost_center(self) -> str:
        if not hasattr(self, "_cc"):
            self._cc = frappe.get_cached_value("Company", self.company, "cost_center")
        return self._cc

    def _company_currency(self) -> str:
        if not hasattr(self, "_cur"):
            self._cur = frappe.get_cached_value(
                "Company", self.company, "default_currency") or "INR"
        return self._cur

    def _stock_uom(self) -> str:
        if not hasattr(self, "_uom"):
            self._uom = frappe.db.get_single_value(
                "Stock Settings", "stock_uom") or "Nos"
        return self._uom

    def _party_account(self, party_type: str) -> str:
        field = ("default_receivable_account" if party_type == "Customer"
                 else "default_payable_account")
        return frappe.get_cached_value("Company", self.company, field)

    @staticmethod
    def _base_currency(customers: list[dict], suppliers: list[dict]) -> str:
        """The Tally base currency: the most common non-empty ledger CurrencyName
        across every party. A book is overwhelmingly single-currency, so the modal
        value is the base; a party whose CurrencyName differs is forex. Returns ""
        when the file carries no currency name (older exports) - the ERPNext-side
        guard then remains the only check."""
        counts = Counter(
            (r.get("CurrencyName") or "").strip()
            for r in (*customers, *suppliers)
            if (r.get("CurrencyName") or "").strip()
        )
        return counts.most_common(1)[0][0] if counts else ""

    def _tally_currency_foreign(self, record: dict) -> bool:
        """True when this party ledger's own CurrencyName differs from the Tally base
        currency. Blank base or blank ledger currency means 'unknown' -> not foreign,
        so a single-currency book (or an export without the field) is never skipped."""
        ccy = (record.get("CurrencyName") or "").strip()
        return bool(self._tally_base_ccy and ccy and ccy != self._tally_base_ccy)

    def _is_foreign_currency_party(self, party_type: str, party: str) -> bool:
        """True when the party's own default currency is set and differs from the
        company currency. Both Customer and Supplier expose ``default_currency``;
        a blank value means 'follow the company', i.e. not foreign."""
        cur = frappe.db.get_value(party_type, party, "default_currency")
        return bool(cur) and cur != self._company_currency()

    def _existing_markers(self) -> set:
        """Markers already posted for this company, so a re-run skips exactly the
        bills it has posted before and fills only the gaps. Reads opening Sales/
        Purchase Invoices and Payment Entries this importer created (identified by
        the remarks marker)."""
        seen: set = set()
        prefix = f"{self._MARKER}:"
        for doctype, extra in (
            ("Sales Invoice", {"is_opening": "Yes"}),
            ("Purchase Invoice", {"is_opening": "Yes"}),
            ("Payment Entry", {}),
        ):
            filters = {"company": self.company, "docstatus": ["<", 2],
                       "remarks": ["like", f"{prefix}%"]}
            filters.update(extra)
            for remark in frappe.get_all(
                    doctype, filters=filters, pluck="remarks") or []:
                if remark and remark.startswith(prefix):
                    seen.add(remark.strip())
        return seen


# ── Opening stock importer ───────────────────────────────────────────────────

class StockOpeningImporter:
    """Posts item opening stock as one submitted 'Opening Stock' Stock Reconciliation.

    Tally stores opening stock on the Stock Item master (``OpeningBalance`` = qty,
    ``OpeningRate`` = valuation). The masters export carries no godown-wise split,
    so all opening stock lands in a single default warehouse. The difference posts
    against 'Temporary Opening - <ABBR>', consistent with the opening-balance JE.
    """

    doctype = "Stock Reconciliation"

    def __init__(self, company: str, abbr: str):
        self.company = company
        self.abbr = abbr

    def run(self, items: list, posting_date: str) -> ImportResult:
        result = ImportResult(self.doctype)
        # Idempotency guard (see OpeningBalanceImporter): one aggregate submitted
        # document, so re-running would double opening stock. Skip if one exists.
        if self._existing_opening_stock():
            result.skipped += 1
            result.add_warning(
                "Opening Stock",
                "opening stock already posted for this company (an Opening Stock "
                "reconciliation exists) - skipped to avoid double-counting. Cancel "
                "the existing reconciliation first if you need to re-import.")
            return result
        warehouse = self._default_warehouse()
        if not warehouse:
            result.add_error("Opening Stock", "no warehouse found to hold opening stock")
            return result

        # Aggregate by item_code: a masters export can list the same Stock Item more
        # than once (Tally names are unique, so duplicate tags are the same item), and
        # all opening stock lands in one warehouse - so two rows for the same item
        # would trip ERPNext's "Same item and warehouse combination should be unique"
        # and fail the whole document. Keep one row per item; on a genuine quantity
        # conflict keep the larger and warn rather than silently picking one.
        by_code: dict[str, dict] = {}
        for it in items:
            raw_qty = it.get("OpeningBalance")
            qty = TallyExtractor._parse_quantity(raw_qty)
            if qty < 0:
                # Tally can carry a negative opening quantity (e.g. oversold stock);
                # an 'Opening Stock' reconciliation cannot hold a negative qty and
                # would fail the whole document. Drop this line with a warning rather
                # than silently or fatally.
                result.add_warning(
                    it["_name"],
                    f"opening stock not posted - Tally reports a negative opening "
                    f"quantity '{raw_qty}'. ERPNext opening stock cannot be negative; "
                    "review the item in Tally and set its opening stock manually.")
                continue
            if qty == 0:
                # A non-empty cell that parses to zero is a real opening quantity we
                # failed to read (e.g. an unexpected format) - surface it instead of
                # dropping the item's opening stock silently.
                if str(raw_qty or "").strip():
                    result.add_warning(
                        it["_name"],
                        f"opening stock not posted - could not read quantity "
                        f"'{raw_qty}'. Set this item's opening stock manually.")
                continue
            # Valuation: prefer the opening rate (unit-suffixed, e.g. "1.00/Nos"), then
            # the value Tally already computed (OpeningValue ÷ qty - sign is direction,
            # so abs), then the item's standard cost. _to_float can't read the unit
            # suffix, hence _parse_rate.
            opening_rate = TallyExtractor._parse_rate(it.get("OpeningRate"))
            opening_value = abs(BaseImporter._to_float(it.get("OpeningValue")))
            rate = opening_rate
            if rate == 0 and opening_value:
                rate = opening_value / qty
            if rate == 0:
                rate = TallyExtractor._parse_rate(it.get("StandardCost"))
            # Item Master Rule 1: when Tally gives BOTH an opening rate and value they
            # must satisfy value = qty x rate (Tally derives one from the other). A
            # divergence beyond rounding means the source is internally inconsistent;
            # we post using the opening rate, but flag that the posted value will not
            # match Tally's recorded value rather than letting it diverge silently.
            if opening_rate and opening_value:
                expected = qty * opening_rate
                if abs(opening_value - expected) > max(1.0, 0.01 * expected):
                    result.add_warning(
                        it["_name"],
                        f"opening stock value does not reconcile - Tally reports a "
                        f"value of {opening_value:,.2f}, but quantity x rate is "
                        f"{qty:g} x {opening_rate:g} = {expected:,.2f}. Posted using "
                        "the opening rate; verify this item's opening stock in Tally.")

            code = safe_item_code(it["_name"])
            prev = by_code.get(code)
            if prev is not None:
                if abs(prev["qty"] - qty) > 1e-9:
                    result.add_warning(
                        it["_name"],
                        f"item appears more than once in the export with different "
                        f"opening quantities ({prev['qty']:g} vs {qty:g}); kept the "
                        f"larger. Verify the item's opening stock in Tally.")
                    if qty <= prev["qty"]:
                        continue
                else:
                    continue  # exact duplicate tag - same item exported twice
            row = {
                "item_code": code,
                "warehouse": warehouse,
                "qty": qty,
                "valuation_rate": rate,
            }
            if rate == 0:
                # ERPNext rejects a positive opening qty at a zero rate
                # ("Valuation Rate required for Item …") unless the row explicitly
                # allows it. Tally itself carries no value for these items, so we
                # post the quantity faithfully at zero value rather than blocking
                # the whole reconciliation. One warning per item, with identical
                # text - the log groups same-reason rows into a single line.
                row["allow_zero_valuation_rate"] = 1
                result.add_warning(
                    it["_name"],
                    "opening stock posted with a zero valuation rate - Tally carries "
                    "no opening rate, value or standard cost for this item, so its "
                    "opening stock has no book value. Set a valuation rate in ERPNext "
                    "if it should carry value.")
            by_code[code] = row
        rows = list(by_code.values())
        if not rows:
            return result  # no opening stock to post

        try:
            doc = frappe.get_doc({
                "doctype": "Stock Reconciliation",
                "purpose": "Opening Stock",
                "company": self.company,
                "posting_date": posting_date,
                "posting_time": "00:00:00",
                "expense_account": company_scoped("Temporary Opening", self.abbr),
                "items": rows,
            })
            doc.insert(ignore_permissions=True)
            doc.submit()
            frappe.db.commit()
            result.add_created(doc.name)
        except Exception as exc:
            result.add_error("Opening Stock", exc)
            frappe.db.rollback()
        return result

    def _existing_opening_stock(self) -> bool:
        """True when a non-cancelled Opening Stock reconciliation exists for the company."""
        return bool(frappe.db.exists("Stock Reconciliation", {
            "company": self.company,
            "purpose": "Opening Stock",
            "docstatus": ["<", 2],   # draft or submitted, not cancelled
        }))

    def _default_warehouse(self) -> str:
        """A non-group warehouse to hold opening stock.

        Prefer Stock Settings' default, then the migrated default warehouse, then
        any leaf warehouse for the company.
        """
        ss = frappe.db.get_single_value("Stock Settings", "default_warehouse")
        # Stock Settings' default is global, so on a multi-company site it can point
        # at another company's warehouse - scope it to ours or the Stock Reconciliation
        # is rejected ("Warehouse X does not belong to company Y").
        if ss and frappe.db.exists(
                "Warehouse", {"name": ss, "is_group": 0, "company": self.company}):
            return ss
        candidate = company_scoped(DEFAULT_WAREHOUSE, self.abbr)
        if frappe.db.exists("Warehouse", {"name": candidate, "is_group": 0}):
            return candidate
        rows = frappe.get_all(
            "Warehouse", filters={"company": self.company, "is_group": 0},
            pluck="name", limit=1,
        )
        return rows[0] if rows else ""


# ── Facade ───────────────────────────────────────────────────────────────────

class ERPNextImporter:
    """
    Stable entry point used by the orchestrator and tests.

    Resolves company metadata once, then delegates each entity to its importer.
    """

    def __init__(self, erpnext_company: str, uom_overrides: dict | None = None,
                 coa_mode: str = "reuse"):
        self.company = erpnext_company
        self.abbr = frappe.get_value("Company", erpnext_company, "abbr") or ""
        self._uom_overrides = uom_overrides or {}
        self._coa_mode = coa_mode

    def import_accounts(self, accounts: list) -> ImportResult:
        return AccountImporter(self.company, self.abbr, mode=self._coa_mode).run(accounts)

    def import_cost_centres(self, centres: list) -> ImportResult:
        return CostCentreImporter(self.company, self.abbr).run(centres)

    def import_opening_balances(self, accounts: list, customers: list,
                                suppliers: list, posting_date: str = "") -> ImportResult:
        with _company_opening_lock(self.company) as got:
            if not got:
                result = ImportResult("Journal Entry")
                result.skipped += 1
                result.add_warning(
                    "Opening Entry",
                    "another migration for this company is posting opening balances "
                    "right now - skipped here to avoid double-posting. Re-run after it "
                    "finishes; already-posted batches are skipped and any gaps filled.")
                return result
            return OpeningBalanceImporter(self.company, self.abbr).run(
                accounts, customers, suppliers, self._opening_date(posting_date))

    def import_party_openings(self, bills: list, customers: list,
                              suppliers: list, posting_date: str = "") -> ImportResult:
        """Post party opening balances invoice-wise (see PartyOpeningImporter).

        Shares the per-company opening lock with the JE/stock openings so two
        concurrent runs can't both post; stands down with a warning when another
        run holds it (its own idempotency markers then fill any gaps on re-run)."""
        with _company_opening_lock(self.company) as got:
            if not got:
                result = ImportResult("Opening Invoice")
                result.skipped += 1
                result.add_warning(
                    "Party Openings",
                    "another migration for this company is posting opening balances "
                    "right now - skipped here to avoid double-posting. Re-run after it "
                    "finishes; already-posted bills are skipped and any gaps filled.")
                return result
            return PartyOpeningImporter(
                self.company, self.abbr, self._opening_date(posting_date)).run(
                    bills, customers, suppliers)

    def import_opening_stock(self, items: list, posting_date: str = "") -> ImportResult:
        with _company_opening_lock(self.company) as got:
            if not got:
                result = ImportResult("Stock Reconciliation")
                result.skipped += 1
                result.add_warning(
                    "Opening Stock",
                    "another migration for this company is posting opening stock right "
                    "now - skipped here to avoid double-counting. Re-run after it "
                    "finishes to fill any gaps.")
                return result
            return StockOpeningImporter(self.company, self.abbr).run(
                items, self._opening_date(posting_date))

    def import_stock_groups(self, groups: list[dict]) -> ImportResult:
        return StockGroupImporter(self.company, self.abbr).run(groups)

    def import_units(self, units: list[dict]) -> ImportResult:
        return UnitImporter(self.company, self.abbr).run(units)

    def import_warehouses(self, warehouses: list[dict]) -> ImportResult:
        return WarehouseImporter(self.company, self.abbr).run(warehouses)

    def import_customers(self, customers: list[dict]) -> ImportResult:
        return CustomerImporter(self.company, self.abbr).run(customers)

    def import_suppliers(self, suppliers: list[dict]) -> ImportResult:
        return SupplierImporter(self.company, self.abbr).run(suppliers)

    def import_items(self, items: list[dict]) -> ImportResult:
        # Heal a prior hard-killed run before reading the setting, then suspend
        # India Compliance HSN validation so items without an HSN still import.
        _restore_hsn_validation()
        with _hsn_validation_suspended():
            return ItemImporter(
                self.company, self.abbr, uom_overrides=self._uom_overrides).run(items)

    def _opening_date(self, posting_date: str = "") -> str:
        """Posting date for opening entries.

        Uses the user-supplied date when given (pre-flight picker); otherwise
        defaults to the company's current fiscal-year start."""
        if posting_date:
            return str(posting_date)
        return self._fiscal_year_start()

    def _fiscal_year_start(self) -> str:
        """Posting date for the opening entry - the company's current FY start."""
        from erpnext.accounts.utils import get_fiscal_year
        return str(get_fiscal_year(frappe.utils.nowdate(), company=self.company)[1])
