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
)
from tally_migrator.naming import safe_item_code
from tally_migrator.validation.engine import infer_gst_category


# ── Result tracking ───────────────────────────────────────────────────────────

@dataclass
class ImportResult:
    doctype: str
    created: int = 0
    skipped: int = 0
    errors: list[dict] = field(default_factory=list)

    def add_error(self, name: str, reason) -> None:
        self.errors.append({"name": name, "reason": str(reason)})

    @property
    def failed(self) -> int:
        return len(self.errors)

    def as_dict(self) -> dict:
        return {
            "created": self.created,
            "skipped": self.skipped,
            "failed": self.failed,
            "errors": self.errors,
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
                self.after_insert(name, record)
        return result

    # ── Overridable hooks ────────────────────────────────────────────────────
    def iter_records(self, records: list[dict]) -> list[dict]:
        return records

    def before_run(self, records: list[dict], result: ImportResult) -> None:
        pass

    def build_doc(self, record: dict) -> dict:
        raise NotImplementedError

    def after_insert(self, name: str, record: dict) -> None:
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
        try:
            existing = frappe.db.get_value(self.doctype, {self.key_field: key_value}, "name")
            if existing:
                result.skipped += 1
                return existing, False
            doc = frappe.get_doc(data)
            doc.insert(ignore_permissions=True)
            frappe.db.commit()
            result.created += 1
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

    def after_insert(self, name: str, record: dict) -> None:
        self._save_address(name, self.doctype, record)

    @staticmethod
    def _gst_category(record: dict) -> str:
        """Infer the ERPNext GST Category from the party's GSTIN + country."""
        return infer_gst_category(
            record.get("GSTRegistrationNumber") or "",
            record.get("CountryName") or "India",
        )

    def _resolve_payment_terms(self, tally_credit_period: str) -> str:
        """
        Tally stores a credit period as '30 Days' or '30'. Map to an ERPNext
        Payment Terms record named 'Net <days>' when one exists.
        """
        if not tally_credit_period:
            return ""
        days = "".join(filter(str.isdigit, tally_credit_period))
        if not days:
            return ""
        candidate = f"Net {days}"
        return candidate if frappe.db.exists("Payment Terms", candidate) else ""

    def _save_address(self, link_name: str, link_type: str, data: dict) -> None:
        """Create a Billing Address linked to the party. Non-fatal on failure."""
        raw_address = (data.get("Address") or "").strip()
        if not raw_address:
            return
        try:
            addr = frappe.new_doc("Address")
            addr.address_title = link_name
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


class CustomerImporter(PartyImporter):
    doctype = "Customer"
    key_field = "customer_name"

    def build_doc(self, record: dict) -> dict:
        return {
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
        self._ensure_item_groups({r.get("Parent") for r in records if r.get("Parent")})

    def build_doc(self, record: dict) -> dict:
        tally_uom = (record.get("BaseUnits") or "").strip()
        # User-supplied overrides (from pre-flight check) take precedence
        uom = self._uom_overrides.get(tally_uom) or UOM_MAP.get(tally_uom, DEFAULT_UOM)
        return {
            "doctype": "Item",
            "item_code": safe_item_code(record["_name"]),
            "item_name": record["_name"],
            "item_group": record.get("Parent") or DEFAULT_ITEM_GROUP,
            "stock_uom": uom,
            "description": record.get("Description") or record["_name"],
            "is_stock_item": 1,
            "standard_rate": self._to_float(record.get("StandardPrice")),
            "valuation_rate": self._to_float(record.get("StandardCost")),
            "gst_hsn_code": record.get("HSNCode") or "",
        }

    def _ensure_item_groups(self, groups: set[str]) -> None:
        """Create any missing Item Groups under the default parent group."""
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


# ── Warehouse importer ──────────────────────────────────────────────────────────

class WarehouseImporter(BaseImporter):
    doctype = "Warehouse"
    key_field = "warehouse_name"

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

        def visit(name: str) -> None:
            if name in visited or name not in index:
                return
            parent = index[name].get("Parent", "").strip()
            if parent and parent in name_set:
                visit(parent)
            visited.add(name)
            ordered.append(index[name])

        for w in warehouses:
            visit(w["_name"])
        return ordered


# ── Facade ───────────────────────────────────────────────────────────────────

class ERPNextImporter:
    """
    Stable entry point used by the orchestrator and tests.

    Resolves company metadata once, then delegates each entity to its importer.
    """

    def __init__(self, erpnext_company: str, uom_overrides: dict | None = None):
        self.company = erpnext_company
        self.abbr = frappe.get_value("Company", erpnext_company, "abbr") or ""
        self._uom_overrides = uom_overrides or {}

    def import_warehouses(self, warehouses: list[dict]) -> ImportResult:
        return WarehouseImporter(self.company, self.abbr).run(warehouses)

    def import_customers(self, customers: list[dict]) -> ImportResult:
        return CustomerImporter(self.company, self.abbr).run(customers)

    def import_suppliers(self, suppliers: list[dict]) -> ImportResult:
        return SupplierImporter(self.company, self.abbr).run(suppliers)

    def import_items(self, items: list[dict]) -> ImportResult:
        return ItemImporter(self.company, self.abbr, uom_overrides=self._uom_overrides).run(items)
