"""
ERPNext importers — Tally masters → ERPNext via Frappe's ORM.

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
subclass — no existing code changes (Open/Closed).

Insert rules (shared by every importer)
---------------------------------------
- Record already exists (matched by ``key_field``)  → skip, never overwrite.
- Insert fails                                       → record error, rollback, continue.
- Per-record commit isolates partial failures so one bad record cannot undo
  successfully imported ones.
"""
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
from tally_migrator.validation.engine import infer_gst_category


# ── Result tracking ───────────────────────────────────────────────────────────

@dataclass
class ImportResult:
    doctype: str
    created: int = 0
    skipped: int = 0
    errors: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    # ERPNext names of the docs this importer actually inserted — the authoritative
    # "what did this run touch" record (incl. the opening JE / Stock Reconciliation),
    # so a migration can be reviewed or reversed by inspection.
    created_names: list[str] = field(default_factory=list)

    def add_created(self, name: str) -> None:
        self.created += 1
        if name:
            self.created_names.append(name)

    def add_error(self, name: str, reason) -> None:
        self.errors.append({"name": name, "reason": str(reason)})

    def add_warning(self, name: str, reason) -> None:
        """Record a *non-fatal* partial drop — the main record imported, but a
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
    # the same ``key_field`` value can legitimately exist in another company —
    # without it, a same-named record in Company A makes Company B's get skipped.
    scope_field: str = ""

    def __init__(self, company: str, abbr: str):
        self.company = company
        self.abbr = abbr

    # ── Template method ─────────────────────────────────────────────────────
    def run(self, records: list[dict]) -> ImportResult:
        result = ImportResult(self.doctype)
        self.before_run(records, result)
        for record in self.iter_records(records):
            name, created = self._upsert(result, self.build_doc(record))
            # after_insert (e.g. address creation) must run ONLY for newly
            # created records — otherwise a re-run duplicates side effects for
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
            result.add_error(key_value, exc)
            frappe.db.rollback()
            return None, False

    # ── Utilities ─────────────────────────────────────────────────────────────
    @staticmethod
    def _to_float(val) -> float:
        try:
            return float(str(val or 0).replace(",", "").strip())
        except (ValueError, TypeError):
            return 0.0


# ── Party importers (Customer / Supplier) ──────────────────────────────────────

class PartyImporter(BaseImporter):
    """Shared behaviour for Customers and Suppliers: billing address + payment terms."""

    def after_insert(self, name: str, record: dict, result: "ImportResult") -> None:
        self._save_address(name, self.doctype, record, result)
        self._save_contact(name, self.doctype, record, result)

    @staticmethod
    def _gst_category(record: dict) -> str:
        """ERPNext GST Category. Tally's explicit registration type wins when set
        (it alone distinguishes Composition / SEZ); otherwise infer from GSTIN +
        country."""
        explicit = gst_category_from_type(record.get("GSTRegistrationType") or "")
        if explicit:
            return explicit
        return infer_gst_category(
            record.get("GSTRegistrationNumber") or "",
            record.get("CountryName") or "India",
        )

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
        """Create a Billing Address linked to the party. Non-fatal on failure —
        but a failure is recorded as a warning so the dropped address is visible
        in the migration log rather than lost silently."""
        raw_address = (data.get("Address") or "").strip()
        if not raw_address:
            return
        try:
            addr = frappe.new_doc("Address")
            addr.address_title = (data.get("MailingName") or "").strip() or link_name
            addr.address_type = "Billing"
            addr.address_line1 = raw_address
            addr.city = data.get("PinCode") or ""          # Tally rarely supplies a city
            addr.state = TALLY_STATE_MAP.get(data.get("LedgerState", ""), "")
            addr.country = data.get("CountryName") or "India"
            addr.pincode = data.get("PinCode") or ""
            addr.phone = data.get("LedgerPhone") or data.get("LedgerMobile") or ""
            addr.email_id = data.get("LedgerEmail") or ""
            addr.gstin = data.get("GSTRegistrationNumber") or ""
            addr.append("links", {"link_doctype": link_type, "link_name": link_name})
            addr.insert(ignore_permissions=True)
            frappe.db.commit()
        except Exception as exc:
            frappe.log_error(f"Address save failed for {link_name}: {exc}", "Tally Migrator")
            result.add_warning(link_name, f"address not created: {exc}")

    def _save_contact(self, link_name: str, link_type: str, data: dict,
                      result: "ImportResult") -> None:
        """Create a Contact (phone / mobile / email) linked to the party.

        Tally keeps these on the ledger, but ERPNext stores them on a Contact, not
        on the Customer/Supplier itself — so without this they'd survive only as
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
            frappe.log_error(f"Contact save failed for {link_name}: {exc}", "Tally Migrator")
            result.add_warning(link_name, f"contact not created: {exc}")


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
        return doc


class SupplierImporter(PartyImporter):
    doctype = "Supplier"
    key_field = "supplier_name"

    def build_doc(self, record: dict) -> dict:
        return {
            "doctype": "Supplier",
            "supplier_name": record["_name"],
            "supplier_group": DEFAULT_SUPPLIER_GROUP,
            "supplier_type": "Company",
            "tax_id": record.get("GSTRegistrationNumber") or "",
            "pan": record.get("INCOMETAXNumber") or "",
            "gst_category": self._gst_category(record),
            "payment_terms": self._resolve_payment_terms(record.get("BillCreditPeriod")),
        }


# ── Item importer ───────────────────────────────────────────────────────────────

class ItemImporter(BaseImporter):
    doctype = "Item"
    key_field = "item_code"

    def __init__(self, company: str, abbr: str, uom_overrides: dict | None = None):
        super().__init__(company, abbr)
        self._uom_overrides = uom_overrides or {}

    def before_run(self, records: list[dict], result: ImportResult) -> None:
        self._ensure_item_groups({r.get("Parent") for r in records if r.get("Parent")}, result)
        # Surface any GST treatment we couldn't map so the loss is auditable rather
        # than silently defaulting the item to taxable.
        for r in records:
            raw = (r.get("GSTTaxability") or "").strip()
            if raw and self._gst_treatment(raw) is None:
                result.add_warning(
                    r["_name"],
                    f"GST type '{raw}' not recognised — item imported as taxable; "
                    "set its GST treatment manually if needed.")

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
        doc.update(self._gst_fields(record))
        return doc

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
                    frappe.log_error(f"Item Group creation failed: {group}: {exc}", "Tally Migrator")
                    result.add_warning(group, f"item group not created: {exc}")


# ── Warehouse importer ──────────────────────────────────────────────────────────

class WarehouseImporter(BaseImporter):
    doctype = "Warehouse"
    key_field = "warehouse_name"
    scope_field = "company"   # warehouse_name is unique per company, not globally

    def iter_records(self, records: list[dict]) -> list[dict]:
        return self._topo_sort(records)

    def build_doc(self, record: dict) -> dict:
        doc = {
            "doctype": "Warehouse",
            "warehouse_name": record["_name"],
            "company": self.company,
            "address_line_1": record.get("Address") or "",
        }
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
            category = self._uom_category()
            if category:
                doc["category"] = category
            frappe.get_doc(doc).insert(ignore_permissions=True)
            frappe.db.commit()
        except Exception as exc:
            frappe.log_error(f"UOM conversion failed for {u['_name']}: {exc}", "Tally Migrator")
            result.add_warning(
                u["_name"],
                f"compound unit conversion (1 {base} = {factor:g} {additional}) "
                f"not created: {exc}")
            frappe.db.rollback()

    @staticmethod
    def _uom_category() -> str:
        """A UOM Category to attach conversions to (required in recent ERPNext).
        Reuse one if present, else create a 'Tally Imported' category."""
        if not frappe.db.has_column("UOM Conversion Factor", "category"):
            return ""
        existing = frappe.get_all("UOM Category", pluck="name", limit=1)
        if existing:
            return existing[0]
        try:
            cat = frappe.get_doc({"doctype": "UOM Category", "category_name": "Tally Imported"})
            cat.insert(ignore_permissions=True)
            frappe.db.commit()
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
    extractor — they are Customers/Suppliers, not ledger Accounts.

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
        # reuse: reserved groups already exist in ERPNext — don't recreate them.
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


# ── Opening balance importer ─────────────────────────────────────────────────

class OpeningBalanceImporter:
    """Posts one balanced 'Opening Entry' Journal Entry for the whole trial balance.

    Three balance sources are combined into a single submitted JE:
      • ledger accounts  — Dr/Cr against the account itself,
      • customers        — against the company's default Receivable account, with
                           ``party_type='Customer'`` / ``party=<name>``,
      • suppliers        — against the default Payable account, ``party=<name>``.

    Referenced accounts/parties are normally created first (COA + Customers +
    Suppliers). A line whose account/party did *not* get created (its earlier import
    failed) is skipped with a warning rather than included — otherwise a single
    missing reference would make the whole submitted entry throw and roll back,
    silently dropping *every* opening balance. ERPNext requires the JE to balance;
    any residual difference
    (e.g. only part of the trial balance was migrated) is absorbed by a balancing
    line against 'Temporary Opening - <ABBR>'. The entry is **submitted** so the
    balances actually post to the General Ledger.
    """

    def __init__(self, company: str, abbr: str):
        self.company = company
        self.abbr = abbr

    def run(self, accounts: list, customers: list, suppliers: list,
            posting_date: str) -> ImportResult:
        result = ImportResult("Journal Entry")
        lines: list[dict] = []
        lines += self._account_lines(accounts, result)
        lines += self._party_lines(customers, "Customer", "default_receivable_account", result)
        lines += self._party_lines(suppliers, "Supplier", "default_payable_account", result)
        if not lines:
            return result  # nothing to post

        self._balance(lines, result)
        try:
            doc = frappe.get_doc({
                "doctype": "Journal Entry",
                "voucher_type": "Opening Entry",
                "posting_date": posting_date,
                "company": self.company,
                "accounts": lines,
                "user_remark": "Opening balances imported from Tally",
            })
            doc.insert(ignore_permissions=True)
            doc.submit()
            frappe.db.commit()
            result.add_created(doc.name)
        except Exception as exc:
            result.add_error("Opening Entry", exc)
            frappe.db.rollback()
        return result

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
                    f"opening balance skipped — account '{account}' was not created "
                    "(its import failed earlier). Fix the account and re-run.")
                continue
            lines.append(self._line(account, node.opening_balance, node.opening_dr_cr))
        return lines

    def _party_lines(self, parties: list, party_type: str, company_field: str,
                     result: ImportResult) -> list[dict]:
        if not parties:
            return []
        control = frappe.get_cached_value("Company", self.company, company_field)
        lines, missing_control = [], False
        for record in parties:
            amount, drcr = TallyExtractor._parse_opening(record.get("OpeningBalance", ""))
            if not amount:
                continue
            if not control:
                missing_control = True
                continue
            if not frappe.db.exists(party_type, record["_name"]):
                result.add_warning(
                    record["_name"],
                    f"opening balance skipped — {party_type} '{record['_name']}' was "
                    "not created (its import failed earlier). Fix it and re-run.")
                continue
            line = self._line(control, amount, drcr)
            line.update({"party_type": party_type, "party": record["_name"]})
            lines.append(line)
        if missing_control:
            result.add_error(
                f"{party_type} opening balances",
                f"company has no {company_field.replace('_', ' ')} set — skipped",
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
        # A non-trivial plug means the migrated balances don't net to zero on their
        # own — usually only part of the trial balance was migrated. ERPNext still
        # balances the entry against Temporary Opening, but that silently hides the
        # gap, so flag it as a non-fatal warning for review.
        if result is not None and abs(diff) >= self.PLUG_NOISE_THRESHOLD:
            side = "credit" if diff > 0 else "debit"
            result.add_warning(
                "Opening Entry",
                f"opening balances did not net to zero; {abs(diff):,.2f} was {side}ed "
                f"to 'Temporary Opening - {self.abbr}' to balance the entry — review "
                "whether the full trial balance was migrated.")


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
        warehouse = self._default_warehouse()
        if not warehouse:
            result.add_error("Opening Stock", "no warehouse found to hold opening stock")
            return result

        rows = []
        for it in items:
            qty = BaseImporter._to_float(it.get("OpeningBalance"))
            if qty <= 0:
                continue
            rate = (BaseImporter._to_float(it.get("OpeningRate"))
                    or BaseImporter._to_float(it.get("StandardCost")))
            rows.append({
                "item_code": safe_item_code(it["_name"]),
                "warehouse": warehouse,
                "qty": qty,
                "valuation_rate": rate,
            })
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

    def _default_warehouse(self) -> str:
        """A non-group warehouse to hold opening stock.

        Prefer Stock Settings' default, then the migrated default warehouse, then
        any leaf warehouse for the company.
        """
        ss = frappe.db.get_single_value("Stock Settings", "default_warehouse")
        if ss and frappe.db.exists("Warehouse", {"name": ss, "is_group": 0}):
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
        return OpeningBalanceImporter(self.company, self.abbr).run(
            accounts, customers, suppliers, self._opening_date(posting_date))

    def import_opening_stock(self, items: list, posting_date: str = "") -> ImportResult:
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
        return ItemImporter(self.company, self.abbr, uom_overrides=self._uom_overrides).run(items)

    def _opening_date(self, posting_date: str = "") -> str:
        """Posting date for opening entries.

        Uses the user-supplied date when given (pre-flight picker); otherwise
        defaults to the company's current fiscal-year start."""
        if posting_date:
            return str(posting_date)
        return self._fiscal_year_start()

    def _fiscal_year_start(self) -> str:
        """Posting date for the opening entry — the company's current FY start."""
        from erpnext.accounts.utils import get_fiscal_year
        return str(get_fiscal_year(frappe.utils.nowdate(), company=self.company)[1])
