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
        ordered, visited = [], set()

        def visit(name: str) -> None:
            if name in visited or name not in index:
                return
            parent = index[name].parent
            if parent in index:
                visit(parent)
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
            frappe.get_doc(doc).insert(ignore_permissions=True)
            frappe.db.commit()
            result.created += 1
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
        ordered, visited = [], set()

        def visit(name: str) -> None:
            if name in visited or name not in index:
                return
            parent = index[name].parent
            if parent in index:
                visit(parent)
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
            frappe.get_doc({
                "doctype": "Cost Center",
                "cost_center_name": node.name,
                "parent_cost_center": parent,
                "company": self.company,
                "is_group": 1 if is_group else 0,
            }).insert(ignore_permissions=True)
            frappe.db.commit()
            result.created += 1
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

    All referenced accounts/parties must already exist (COA + Customers + Suppliers
    imported first). ERPNext requires the JE to balance; any residual difference
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
        lines += self._account_lines(accounts)
        lines += self._party_lines(customers, "Customer", "default_receivable_account", result)
        lines += self._party_lines(suppliers, "Supplier", "default_payable_account", result)
        if not lines:
            return result  # nothing to post

        self._balance(lines)
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
            result.created += 1
        except Exception as exc:
            result.add_error("Opening Entry", exc)
            frappe.db.rollback()
        return result

    # ── Line builders ────────────────────────────────────────────────────────
    def _account_lines(self, accounts: list) -> list[dict]:
        lines = []
        for node in accounts:
            if not node.opening_balance or node.is_group:
                continue
            lines.append(self._line(
                company_scoped(node.name, self.abbr),
                node.opening_balance, node.opening_dr_cr,
            ))
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

    def _balance(self, lines: list[dict]) -> None:
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
            result.created += 1
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
                                suppliers: list) -> ImportResult:
        return OpeningBalanceImporter(self.company, self.abbr).run(
            accounts, customers, suppliers, self._fiscal_year_start())

    def import_opening_stock(self, items: list) -> ImportResult:
        return StockOpeningImporter(self.company, self.abbr).run(items, self._fiscal_year_start())

    def import_warehouses(self, warehouses: list[dict]) -> ImportResult:
        return WarehouseImporter(self.company, self.abbr).run(warehouses)

    def import_customers(self, customers: list[dict]) -> ImportResult:
        return CustomerImporter(self.company, self.abbr).run(customers)

    def import_suppliers(self, suppliers: list[dict]) -> ImportResult:
        return SupplierImporter(self.company, self.abbr).run(suppliers)

    def import_items(self, items: list[dict]) -> ImportResult:
        return ItemImporter(self.company, self.abbr, uom_overrides=self._uom_overrides).run(items)

    def _fiscal_year_start(self) -> str:
        """Posting date for the opening entry — the company's current FY start."""
        from erpnext.accounts.utils import get_fiscal_year
        return str(get_fiscal_year(frappe.utils.nowdate(), company=self.company)[1])
