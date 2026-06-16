"""ERPNextImporter facade: orchestrates every entity importer in order."""

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
from .base import ImportResult, BaseImporter
from .party import PartyImporter, CustomerImporter, SupplierImporter
from .items import ItemImporter
from .warehouses import WarehouseImporter, StockGroupImporter
from .units import UnitImporter
from .prices import PriceImporter
from .bom import BomImporter
from .accounts import AccountImporter, CostCentreImporter
from .hsn import _restore_hsn_validation, _hsn_validation_suspended
from .openings import (
    _company_opening_lock,
    OpeningBalanceImporter,
    PartyOpeningImporter,
    StockOpeningImporter,
)

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

    def import_prices(self, items: list[dict]) -> ImportResult:
        return PriceImporter(self.company, self.abbr).run(items)

    def import_boms(self, items: list[dict]) -> ImportResult:
        return BomImporter(self.company, self.abbr).run(items)

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
