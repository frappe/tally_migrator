"""Party importers: Customer and Supplier (shared address / payment-term handling)."""

import contextlib
import importlib
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache

import frappe
from frappe.utils import validate_email_address

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
from tally_migrator.migration import profiler as _profiler
from tally_migrator.tally.extractors import TallyExtractor
from tally_migrator.validation.engine import (
    infer_gst_category, validate_gstin, GSTIN_STATE_CODES,
)
from .base import BaseImporter, ImportResult, atomic
from .banks import _ensure_bank, _insert_bank_account


# India Compliance's PAN format (income_tax_india): 5 letters, 4 digits, 1 letter.
# Mirrored here so an invalid Tally INCOMETAXNumber can be dropped before it reaches
# IC's validation and blocks the party - see PartyImporter._valid_pan.
_PAN_NUMBER = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")


@lru_cache(maxsize=1)
def _address_requires_state() -> bool:
    """Whether a stateless Indian Address is rejected on this site. India Compliance
    adds the ``gst_state`` field and a validate hook that makes ``state`` mandatory
    for an Indian address; without IC, ERPNext core saves a stateless address fine.
    So the importer should only pre-skip a no-state address when IC is enforcing -
    otherwise it would drop addresses that would have imported. Cached: the installed
    app set does not change within a run."""
    try:
        return bool(frappe.get_meta("Address").has_field("gst_state"))
    except Exception:
        return False

# ── Gravatar lookup suspension ─────────────────────────────────────────────────
# Frappe's Contact.validate fills a blank avatar by calling has_gravatar(email),
# which makes a live HTTPS GET to gravatar.com *per contact that carries an email*.
# On a real Tally book (tens of thousands of parties) that one network round-trip
# (~35ms each) dominates the party import - measured at ~50% of the whole phase -
# and contributes nothing to the imported data: the avatar URL is purely cosmetic,
# and Frappe itself skips the lookup during bulk import (see has_gravatar's
# `frappe.flags.in_import` guard). We suspend just that lookup for the party phase,
# rather than setting the blunt `in_import` flag (which would also skip doctype
# default-value population and link validation - a real data-quality change). The
# contact still saves identically; it simply gets no gravatar-guessed avatar, the
# same outcome Frappe gives every bulk-imported contact.
@contextlib.contextmanager
def _gravatar_lookup_suspended():
    """Neutralise the per-contact gravatar network lookup for the duration of the
    party import, then restore it. No-op-safe: if the symbol ever moves, we leave
    Frappe untouched. Migrations are serialised by the single-active-run guard, so
    the process-local patch can't bleed into a concurrent migration."""
    try:
        import frappe.contacts.doctype.contact.contact as _contact_mod
    except Exception:
        yield
        return
    original = getattr(_contact_mod, "has_gravatar", None)
    if original is None:
        yield
        return
    _contact_mod.has_gravatar = lambda *args, **kwargs: ""
    try:
        yield
    finally:
        _contact_mod.has_gravatar = original


# ── Per-contact integration hooks suspended for the party phase ─────────────────
# Beyond gravatar, Frappe/ERPNext fire Contact doc-event hooks on every insert that
# do nothing for a migration but cost a DB round-trip each - and, if the integration
# is configured, a per-contact NETWORK call (the same class of cost gravatar had). On
# a real book (tens of thousands of contacts) that is pure overhead, and it dominates
# on Frappe Cloud where every round-trip carries network latency. We neutralise just
# these named handlers for the party phase; the contact still saves identically, it is
# simply not pushed to Google or back-linked to call logs - neither of which a
# migration ever wants to do per record. The handlers are resolved by dotted path at
# event time (frappe.get_attr), so replacing the function on its own module is seen.
_SUSPENDED_PARTY_HOOKS = (
    # Push every new/updated Contact up to the user's Google account. Never wanted
    # mid-migration: it would sync 10k+ contacts and hit Google's API rate limits.
    ("frappe.integrations.doctype.google_contacts.google_contacts",
     ("insert_contacts_to_google_contacts", "update_contacts_to_google_contacts")),
    # Back-link a new Contact to historical Call Logs by phone match - a JOIN scan
    # per contact. A cosmetic telephony convenience, re-derivable; irrelevant here.
    ("erpnext.telephony.doctype.call_log.call_log",
     ("link_existing_conversations",)),
    # India Compliance GSTIN autofill: when GST Settings has the API + autofill on,
    # validating a party with a GSTIN calls the GST portal to fetch its registered
    # name/category (and enqueues request-log jobs). IC already skips this under
    # `frappe.flags.in_import`, which the migrator deliberately does NOT set (it would
    # also skip default population + link validation). Left active it fires once per
    # party: on a 13k-supplier book that is 13k GST-portal fetches whose log jobs
    # saturate the background queue ("Too many queued background jobs"), stalling the
    # run near the end of the party phase. Patched to return None so the gate
    # (`is_autofill_party_info_enabled()`) reads falsy - exactly as if autofill were
    # off - and IC falls back to the offline `guess_gst_category`. The party still
    # imports with its GSTIN; the user can refresh portal info later if they want it.
    ("india_compliance.gst_india.overrides.party",
     ("is_autofill_party_info_enabled",)),
)


@contextlib.contextmanager
def _party_side_effect_hooks_suspended():
    """Neutralise per-party external side-effects for the duration of the party phase,
    then restore them: the Google-Contacts sync, Call-Log linking, and India
    Compliance's per-party GST-portal autofill (see the table above). No-op-safe: a
    module or symbol
    that is not installed is skipped, leaving Frappe/ERPNext untouched. Migrations are
    serialised by the single-active-run guard, so the process-local patch can never
    bleed into a concurrent migration (same contract as _gravatar_lookup_suspended)."""
    saved = []
    for module_path, names in _SUSPENDED_PARTY_HOOKS:
        try:
            mod = importlib.import_module(module_path)
        except Exception:
            continue
        for nm in names:
            if hasattr(mod, nm):
                saved.append((mod, nm, getattr(mod, nm)))
                setattr(mod, nm, lambda *a, **k: None)
    try:
        yield
    finally:
        for mod, nm, fn in saved:
            setattr(mod, nm, fn)


# ── Party importers (Customer / Supplier) ──────────────────────────────────────

class PartyImporter(BaseImporter):
    """Shared behaviour for Customers and Suppliers: billing address + payment terms."""

    # ── Party group derivation (set by each subclass) ──────────────────────────
    # ERPNext stores a party's group in a separate doctype (Customer Group /
    # Supplier Group). Tally states the group on the ledger itself (its PARENT,
    # e.g. "Trade Debtors - Domestic"), so we recreate that group as a leaf under
    # the standard root and assign it - mirroring how ItemImporter recreates Item
    # Groups from an item's Parent. A blank/uncreatable group falls back to the
    # standard default, so a party is never left without a (valid) group.
    group_doctype: str = ""        # "Customer Group" / "Supplier Group"
    group_name_field: str = ""     # "customer_group_name" / "supplier_group_name"
    group_parent_field: str = ""   # "parent_customer_group" / "parent_supplier_group"
    group_root: str = ""           # standard root group to nest new leaves under
    default_group: str = ""        # fallback when Tally carries no usable group

    def run(self, records: list[dict], on_progress=None) -> "ImportResult":
        """Import parties with the per-contact integration hooks suspended - the
        gravatar network lookup plus the Google-Contacts sync and Call-Log linking
        hooks, all data-irrelevant per-record cost of the phase (see
        ``_gravatar_lookup_suspended`` / ``_party_side_effect_hooks_suspended``).
        Everything else is the standard template."""
        with _gravatar_lookup_suspended(), _party_side_effect_hooks_suspended():
            return super().run(records, on_progress=on_progress)

    def before_run(self, records: list[dict], result: "ImportResult") -> None:
        self._ensure_party_groups(
            {(r.get("Parent") or "").strip() for r in records}, result)

    def _ensure_party_groups(self, names: set, result: "ImportResult") -> None:
        """Create any missing party groups (as leaves under the standard root).

        Best-effort and non-fatal: a group that can't be created just means those
        parties fall back to ``default_group`` (recorded as a warning so the lost
        grouping is visible), never a failed party. A group we actually create is
        recorded on the manifest so revert removes it too - the parties that
        reference it are deleted first (same bucket, reversed order), and the delete
        is unforced, so a group still used by a party outside this run is kept."""
        if not self.group_doctype:
            return
        for name in names:
            if not name or name == self.default_group:
                continue
            if frappe.db.exists(self.group_doctype, name):
                continue
            try:
                doc = frappe.new_doc(self.group_doctype)
                doc.set(self.group_name_field, name)
                # A party group must be a leaf - ERPNext rejects assigning a group
                # node to a party - so create it flat under the standard root.
                doc.set(self.group_parent_field, self.group_root)
                doc.is_group = 0
                doc.insert(ignore_permissions=True)
                frappe.db.commit()
                result.add_created(doc.name, self.group_doctype)
            except Exception as exc:
                frappe.db.rollback()
                frappe.log_error(
                    "Tally Migrator", f"{self.group_doctype} creation failed: {name}: {exc}")
                result.add_warning(
                    name,
                    f"{self.group_doctype.lower()} '{name}' not created; parties in it "
                    f"fall back to '{self.default_group}': {exc}")

    def _resolve_group(self, record: dict) -> str:
        """The party's ERPNext group: its Tally PARENT when that group exists (it was
        created in ``before_run``), else the standard default."""
        name = (record.get("Parent") or "").strip()
        if name and self.group_doctype and frappe.db.exists(self.group_doctype, name):
            return name
        return self.default_group

    def after_insert(self, name: str, record: dict, result: "ImportResult") -> None:
        with _profiler.op("address"):
            address_name = self._save_address(name, self.doctype, record, result)
        with _profiler.op("extra_addresses"):
            self._save_extra_addresses(name, self.doctype, record, result)
        with _profiler.op("contact"):
            contact_name = self._save_contact(name, self.doctype, record, result)
        with _profiler.op("extra_contacts"):
            self._save_extra_contacts(name, self.doctype, record, result)
        with _profiler.op("bank"):
            self._save_bank_account(name, self.doctype, record, result)
        # ERPNext does not back-populate the party's primary_* fields when an Address/
        # Contact is inserted with links: Customer.create_primary_address only fires
        # when the Customer doc itself carries address_line1, which a migrated party
        # never does (addresses are created here, after the party). So mark the main
        # billing address / primary contact explicitly and link them on the party,
        # or every migrated party shows no primary address/contact.
        with _profiler.op("primary_links"):
            self._set_primary_links(name, address_name, contact_name, result)

    def _set_primary_links(self, party_name: str, address_name: str,
                           contact_name: str, result: "ImportResult") -> None:
        """Flag the primary Address/Contact and write the party's primary_* links.

        Both halves are required (see after_insert): the ``is_primary_*`` flag alone
        only enforces uniqueness among the party's addresses; it does not populate
        ``<party>_primary_address`` / ``_primary_contact``. Non-fatal - a failure is a
        warning, never a failed party."""
        prefix = frappe.scrub(self.doctype)        # "customer" / "supplier"
        try:
            # Savepointed so a link failure rolls back only these set_values, leaving
            # the party (and its address/contact) intact - the batch commit in run()
            # persists them. Same best-effort contract as before, minus the per-party
            # fsync.
            with atomic():
                if address_name:
                    frappe.db.set_value("Address", address_name, "is_primary_address", 1,
                                        update_modified=False)
                    frappe.db.set_value(self.doctype, party_name,
                                        f"{prefix}_primary_address", address_name,
                                        update_modified=False)
                    from frappe.contacts.doctype.address.address import get_address_display
                    frappe.db.set_value(self.doctype, party_name, "primary_address",
                                        get_address_display(address_name),
                                        update_modified=False)
                if contact_name:
                    frappe.db.set_value("Contact", contact_name, "is_primary_contact", 1,
                                        update_modified=False)
                    frappe.db.set_value(self.doctype, party_name,
                                        f"{prefix}_primary_contact", contact_name,
                                        update_modified=False)
        except Exception as exc:
            frappe.log_error("Tally Migrator", f"Primary link failed for {party_name}: {exc}")
            result.add_warning(
                party_name, f"primary address/contact link not set: {exc}")

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
                with atomic():
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
                with atomic():
                    contact = frappe.new_doc("Contact")
                    contact.first_name = (c.get("name") or "").strip() or link_name
                    contact.append("phone_nos", {
                        "phone": phone,
                        "is_primary_mobile_no": 1 if c.get("whatsapp") else 0,
                    })
                    contact.append("links", {"link_doctype": link_type, "link_name": link_name})
                    contact.insert(ignore_permissions=True)
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

    def _valid_pan(self, raw) -> str:
        """Tally's INCOMETAXNumber as an ERPNext PAN, but only if it is actually a
        PAN. India Compliance validates the field against ^[A-Z]{5}[0-9]{4}[A-Z]$
        and rejects the whole party on a mismatch - and Tally books routinely carry
        a TAN, a typo, or plain digits in this field (e.g. '4870030501'). Storing
        such a value is wrong data and, worse, loses the entire party. So normalise
        (strip + upper) and keep it only when it matches the PAN format; otherwise
        return blank, so the party imports cleanly without a bad PAN, exactly as an
        invalid GSTIN falls back in _maybe_gstin. Mirrors IC's own regex rather than
        importing it, so it degrades gracefully when IC is not installed."""
        pan = (raw or "").strip().upper()
        return pan if _PAN_NUMBER.match(pan) else ""

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
                      result: "ImportResult") -> str:
        """Create a Billing Address linked to the party. Returns the created Address
        name (so the caller can mark it the party's primary address), or "" when no
        address was created. Non-fatal on failure - a failure is recorded as a warning
        so the dropped address is visible in the migration log rather than lost."""
        raw_address = (data.get("Address") or "").strip()
        if not raw_address:
            return ""
        state = self._resolve_state(data)
        country = (data.get("CountryName") or "").strip() or self.company_country
        # India Compliance hard-requires a state on an Indian address. When Tally
        # gave us nothing to derive one from (no ledger state, valid GSTIN or PIN),
        # the insert is certain to fail - so skip it with a single warning rather
        # than attempt a doomed insert that rolls back and floods the Error Log on a
        # large import. Only when IC is actually enforcing the rule (without it, core
        # saves a stateless address). Anything with a derivable state imports below.
        if country == "India" and not state and _address_requires_state():
            result.add_warning(
                link_name, "address not created - the Tally data has no state "
                "(add a state, GSTIN or PIN code in Tally, then re-import)")
            return ""
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
            addr.state = state
            addr.country = country
            addr.pincode = data.get("PinCode") or ""
            addr.phone = data.get("LedgerPhone") or data.get("LedgerMobile") or ""
            # A malformed email (Tally holds plenty: "NA", "n/a", "x@") makes ERPNext
            # reject the whole address. Run it through the same validator ERPNext uses
            # and drop just the bad email, so the address still imports.
            addr.email_id = validate_email_address(
                (data.get("LedgerEmail") or "").strip(), throw=False)
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
            # Savepointed: a failed insert rolls back only the address, never the
            # party or the batch. The batch commit in run() persists it.
            with atomic():
                addr.insert(ignore_permissions=True)
            return addr.name
        except Exception as exc:
            # India Compliance hard-rejects a pincode whose leading digits don't match
            # the state ("Postal Code X ... is not associated with <State>"). The
            # pre-flight already warns "PIN and state to verify", so rather than lose
            # the whole address, drop just the suspect PIN and retry once - mirroring
            # how we drop a rejected GSTIN above. Keeps the address; flags the PIN.
            # The first attempt's savepoint has already rolled back.
            msg = str(exc).lower()
            if addr.pincode and ("not associated with" in msg or "postal code" in msg):
                try:
                    addr.pincode = ""
                    # Force the name to be re-derived on this second attempt. The first
                    # insert already ran autoname - which, for a colliding address title,
                    # bumps a "<title>-<type>-.#" series counter (make_autoname) - then
                    # failed, and atomic()'s savepoint rolled the counter back. But the
                    # doc kept the name it claimed (flags.name_set short-circuits
                    # re-naming in Document.set_new_name), so reusing it would commit a
                    # name one ahead of the now-rolled-back counter, leaving the series
                    # permanently behind. The NEXT same-titled address then regenerates
                    # this number and hits a duplicate-key error (the address is lost).
                    # Clearing name + name_set makes the retry re-run autoname, bumping
                    # the counter back in step with what actually commits.
                    addr.name = None
                    addr.flags.name_set = False
                    with atomic():
                        addr.insert(ignore_permissions=True)
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
                    return addr.name
                except Exception as exc2:
                    exc = exc2   # retry's savepoint already rolled back
            frappe.log_error("Tally Migrator", f"Address save failed for {link_name}: {exc}")
            result.add_warning(link_name, f"address not created: {exc}")
            return ""

    def _resolve_state(self, data: dict) -> str:
        """The party's ERPNext state, most-reliable signal first: Tally's ledger
        state, else a structurally valid GSTIN's state code, else derived from the
        party's PIN code (India Compliance's pincode<->state map). Tally does not
        mandate a ledger state so most exports omit it, but a PIN is common and
        yields an IC-valid state - so the address is kept rather than dropped on
        India Compliance's missing-state rule."""
        state = TALLY_STATE_MAP.get((data.get("LedgerState") or "").strip(), "")
        if state:
            return state
        gstin = (data.get("GSTRegistrationNumber") or "").strip().upper()
        if gstin and validate_gstin(gstin)[0]:
            derived = GSTIN_STATE_CODES.get(gstin[:2], "")
            if derived:
                return derived
        return self._state_from_pincode((data.get("PinCode") or "").strip())

    def _save_contact(self, link_name: str, link_type: str, data: dict,
                      result: "ImportResult") -> str:
        """Create a Contact (phone / mobile / email) linked to the party. Returns the
        created Contact name (so the caller can mark it the party's primary contact),
        or "" when none was created.

        Tally keeps these on the ledger, but ERPNext stores them on a Contact, not
        on the Customer/Supplier itself - so without this they'd survive only as
        Address fields and be lost entirely when the party has no street address.
        Non-fatal: a failure is recorded as a warning so the dropped contact is
        visible in the migration log rather than lost silently."""
        phone = (data.get("LedgerPhone") or "").strip()
        mobile = (data.get("LedgerMobile") or "").strip()
        # Validate emails up front (the same validator ERPNext's Contact uses) and
        # drop a malformed one - Tally holds plenty ("NA", "n/a", "x@") and a single
        # bad email would otherwise reject the whole contact. Warn so the drop shows.
        raw_email = (data.get("LedgerEmail") or "").strip()
        email = validate_email_address(raw_email, throw=False)
        email_cc = validate_email_address((data.get("EmailCC") or "").strip(), throw=False)
        if raw_email and not email:
            result.add_warning(link_name, "contact email skipped - not a valid email address")
        if not (phone or mobile or email or email_cc):
            return ""
        try:
            with atomic():
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
            return contact.name
        except Exception as exc:
            frappe.log_error("Tally Migrator", f"Contact save failed for {link_name}: {exc}")
            result.add_warning(link_name, f"contact not created: {exc}")
            return ""

    def _save_bank_account(self, link_name: str, link_type: str, data: dict,
                           result: "ImportResult") -> None:
        """Create a Bank Account (account no + IFSC) linked to the party.

        Tally stores a party's bank details on the ledger; ERPNext keeps them on a
        Bank Account doc linked to the Customer/Supplier. Non-fatal - a failure is a
        warning, so the dropped bank detail is visible in the log, not lost."""
        acc_no = (data.get("BankAccountNo") or "").strip()
        if not acc_no:
            return
        bank = _ensure_bank(data.get("BankName") or "", result)
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
    group_doctype = "Customer Group"
    group_name_field = "customer_group_name"
    group_parent_field = "parent_customer_group"
    group_root = "All Customer Groups"
    default_group = DEFAULT_CUSTOMER_GROUP

    def build_doc(self, record: dict) -> dict:
        doc = {
            "doctype": "Customer",
            "customer_name": record["_name"],
            "customer_group": self._resolve_group(record),
            "territory": DEFAULT_TERRITORY,
            "customer_type": "Company",
            "tax_id": record.get("GSTRegistrationNumber") or "",
            "pan": self._valid_pan(record.get("INCOMETAXNumber")),
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
    group_doctype = "Supplier Group"
    group_name_field = "supplier_group_name"
    group_parent_field = "parent_supplier_group"
    group_root = "All Supplier Groups"
    default_group = DEFAULT_SUPPLIER_GROUP

    def build_doc(self, record: dict) -> dict:
        doc = {
            "doctype": "Supplier",
            "supplier_name": record["_name"],
            "supplier_group": self._resolve_group(record),
            "supplier_type": "Company",
            "tax_id": record.get("GSTRegistrationNumber") or "",
            "pan": self._valid_pan(record.get("INCOMETAXNumber")),
            "gst_category": self._gst_category(record),
            "payment_terms": self._resolve_payment_terms(record.get("BillCreditPeriod")),
        }
        doc.update(self._maybe_gstin(record))
        return doc
