"""Warehouse and Stock Group importers (parent-before-child ordering)."""

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
from .base import BaseImporter, ImportResult

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
