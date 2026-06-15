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

from .base import ImportResult, BaseImporter
from .banks import _ensure_bank, _insert_bank_account
from .party import PartyImporter, CustomerImporter, SupplierImporter
from .items import ItemImporter
from .warehouses import WarehouseImporter, StockGroupImporter
from .units import UnitImporter
from .prices import PriceImporter
from .accounts import AccountImporter, CostCentreImporter
from .hsn import (
    _HSN_GUARD_KEY,
    _hsn_validation_field_present,
    _set_hsn_validation,
    _hsn_marker_key,
    _restore_hsn_validation,
    _hsn_validation_suspended,
)
from .openings import (
    _OPENING_LOCK_TTL,
    _company_opening_lock,
    OpeningBalanceImporter,
    PartyOpeningImporter,
    StockOpeningImporter,
)
from .orchestrator import ERPNextImporter
