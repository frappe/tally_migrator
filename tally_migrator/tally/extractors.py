from dataclasses import dataclass
from .client import TallyClient
from .mappings import DEBTOR_ROOTS, CREDITOR_ROOTS


# ── Field lists sent to Tally via TDL FETCH ───────────────────────────────────

LEDGER_FIELDS = [
    "Name", "Parent", "Address", "GSTRegistrationNumber",
    "INCOMETAXNumber", "OpeningBalance", "BillCreditPeriod",
    "LedgerPhone", "LedgerMobile", "LedgerEmail",
    "CountryName", "LedgerState", "PinCode",
]

ITEM_FIELDS = [
    "Name", "Parent", "BaseUnits", "StandardCost", "StandardPrice",
    "OpeningBalance", "OpeningRate", "Description",
    "HSNCode", "GST_Applicable", "GSTTypeName",
]

GODOWN_FIELDS = ["Name", "Parent", "Address"]
GROUP_FIELDS  = ["Name", "Parent"]


@dataclass
class ExtractedMasters:
    customers:  list[dict]
    suppliers:  list[dict]
    items:      list[dict]
    warehouses: list[dict]

    @property
    def summary(self) -> dict:
        return {
            "customers":  len(self.customers),
            "suppliers":  len(self.suppliers),
            "items":      len(self.items),
            "warehouses": len(self.warehouses),
        }


class TallyExtractor:
    """
    Pulls all V1 master data from Tally in a single extraction pass.

    Strategy
    --------
    1. Fetch all Groups → build full descendant tree for Debtors / Creditors.
    2. Fetch all Ledgers once → split into Customers and Suppliers by group ancestry.
    3. Fetch Stock Items and Godowns independently.
    """

    def __init__(self, client: TallyClient):
        self.client = client

    def extract_all(self) -> ExtractedMasters:
        groups  = self.client.get_collection("Group", GROUP_FIELDS)
        ledgers = self.client.get_collection("Ledger", LEDGER_FIELDS)

        debtor_groups   = self._descendants(groups, DEBTOR_ROOTS)
        creditor_groups = self._descendants(groups, CREDITOR_ROOTS)

        return ExtractedMasters(
            customers  = self._filter_ledgers(ledgers, debtor_groups),
            suppliers  = self._filter_ledgers(ledgers, creditor_groups),
            items      = self.client.get_collection("Stock Item", ITEM_FIELDS),
            warehouses = self.client.get_collection("Godown", GODOWN_FIELDS),
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _descendants(self, groups: list[dict], roots: set[str]) -> set[str]:
        """
        BFS over the Group tree to collect all groups under the given roots.
        Handles arbitrary nesting depth (e.g. Sundry Debtors → Retail → Wholesale).
        """
        result, changed = set(roots), True
        while changed:
            changed = False
            for g in groups:
                if g["_name"] not in result and g.get("Parent", "").strip() in result:
                    result.add(g["_name"])
                    changed = True
        return result

    def _filter_ledgers(self, ledgers: list[dict], groups: set[str]) -> list[dict]:
        return [l for l in ledgers if l.get("Parent", "").strip() in groups]
