from dataclasses import dataclass, field
from .mappings import DEBTOR_ROOTS, CREDITOR_ROOTS, TALLY_ROOT_PARENT, TALLY_SYSTEM_LEDGERS, classify_group
from .resolver import ACCOUNT, LedgerResolver


# ── Field lists sent to Tally via TDL FETCH ───────────────────────────────────

LEDGER_FIELDS = [
    "Name", "Parent", "Address", "GSTRegistrationNumber",
    "INCOMETAXNumber", "OpeningBalance", "BillCreditPeriod",
    "LedgerPhone", "LedgerMobile", "LedgerEmail",
    "CountryName", "LedgerState", "PinCode",
    # P1 standard fields Tally states explicitly on the party ledger.
    "GSTRegistrationType", "CreditLimit", "EmailCC", "LedgerContact", "MailingName",
]

ITEM_FIELDS = [
    "Name", "Parent", "BaseUnits", "StandardCost", "StandardPrice",
    "OpeningBalance", "OpeningRate", "Description",
    "HSNCode", "GST_Applicable", "GSTTypeName", "TypeOfSupply",
]

GODOWN_FIELDS     = ["Name", "Parent", "Address"]
GROUP_FIELDS      = ["Name", "Parent"]
COSTCENTRE_FIELDS = ["Name", "Parent"]


# ── Real-Tally tag aliases ────────────────────────────────────────────────────
# A genuine Tally Prime "Export Masters (XML)" emits different tag names than the
# flat ones above for several standard fields — and wraps some in ``.LIST``
# containers. The parser tries the canonical tag first, then these fallbacks, so
# both a real export and the legacy hand-authored sample import correctly. Each
# entry maps a FETCH field → extra tag-path candidates (a dict with ``join``
# concatenates repeated nodes, e.g. multi-line addresses).
_ADDRESS_LIST = {"path": "ADDRESS.LIST/ADDRESS", "join": ", "}

LEDGER_ALIASES = {
    "Address":     [_ADDRESS_LIST],
    "LedgerEmail": ["EMAIL"],            # real Tally uses <EMAIL>, not <LEDGEREMAIL>
    "LedgerState": ["LEDSTATENAME", "STATENAME"],  # not <LEDGERSTATE>
    "LedgerContact": ["CONTACTPERSON"],  # contact-person name on the ledger
    "MailingName": ["MAILINGNAME.LIST/MAILINGNAME"],  # billing/display name
}

ITEM_ALIASES = {
    # Tally keeps standard price/cost as dated revision lists; take the latest.
    "StandardPrice": ["STANDARDPRICELIST.LIST/RATE"],
    "StandardCost":  ["STANDARDCOSTLIST.LIST/RATE"],
}

GODOWN_ALIASES = {
    "Address": [_ADDRESS_LIST],
}


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


@dataclass
class AccountNode:
    """One ERPNext Account to create — a Tally group (is_group) or ledger."""
    name: str
    parent: str            # immediate Tally parent group ("" for a primary group)
    is_group: bool
    root_type: str         # ERPNext root_type (Asset/Liability/Income/Expense/Equity)
    account_type: str      # ERPNext account_type ("" = ordinary)
    is_reserved: bool      # True for Tally's built-in primary groups
    opening_balance: float = 0.0
    opening_dr_cr: str = ""    # "Dr" | "Cr" | ""


@dataclass
class CostCentreNode:
    name: str
    parent: str            # Tally parent centre ("" if top level)


@dataclass
class ExtractedCOA:
    accounts:     list[AccountNode]
    cost_centres: list[CostCentreNode]
    # Ledgers deliberately or unavoidably left out of the COA, with a reason, so
    # nothing is dropped silently. Parties (Customers/Suppliers) are NOT listed
    # here — they are migrated through ``extract_all`` and counted there.
    excluded:     list[dict] = field(default_factory=list)

    @property
    def summary(self) -> dict:
        groups = sum(1 for a in self.accounts if a.is_group)
        return {
            "account_groups":  groups,
            "ledger_accounts": len(self.accounts) - groups,
            "cost_centres":    len(self.cost_centres),
            "excluded_ledgers": len(self.excluded),
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

    def __init__(self, client):
        # ``client`` is any source exposing ``get_collection(obj_type, fields)``
        # — currently FileTallySource (an uploaded Tally masters XML export).
        self.client = client

    def extract_all(self) -> ExtractedMasters:
        groups  = self.client.get_collection("Group", GROUP_FIELDS)
        ledgers = self.client.get_collection("Ledger", LEDGER_FIELDS, LEDGER_ALIASES)

        debtor_groups   = self._descendants(groups, DEBTOR_ROOTS)
        creditor_groups = self._descendants(groups, CREDITOR_ROOTS)

        return ExtractedMasters(
            customers  = self._filter_ledgers(ledgers, debtor_groups),
            suppliers  = self._filter_ledgers(ledgers, creditor_groups),
            items      = self.client.get_collection("Stock Item", ITEM_FIELDS, ITEM_ALIASES),
            warehouses = self.client.get_collection("Godown", GODOWN_FIELDS, GODOWN_ALIASES),
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

    # ── Chart of Accounts ──────────────────────────────────────────────────────

    def extract_coa(self) -> ExtractedCOA:
        """Extract the full Chart of Accounts + Cost Centres.

        Ledgers under Sundry Debtors/Creditors are excluded — they migrate as
        Customers/Suppliers (handled separately), not as ledger Accounts.
        """
        groups       = self.client.get_collection("Group", GROUP_FIELDS)
        ledgers      = self.client.get_collection("Ledger", LEDGER_FIELDS, LEDGER_ALIASES)
        cost_centres = self.client.get_collection("Cost Centre", COSTCENTRE_FIELDS)
        return self._build_coa(groups, ledgers, cost_centres)

    def _build_coa(self, groups, ledgers, cost_centres) -> ExtractedCOA:
        resolver = LedgerResolver(groups, ledgers)
        accounts: list[AccountNode] = []
        excluded: list[dict] = []

        # Group nodes (preserve the tree; account_type stays blank on groups).
        for g in groups:
            name = g["_name"]
            nature = resolver.group_nature(name)
            accounts.append(AccountNode(
                name=name,
                parent=self._norm_parent(g.get("Parent", "")),
                is_group=True,
                root_type=nature["root"],
                account_type="",
                is_reserved=classify_group(name) is not None,
            ))

        # Ledger nodes (skip parties — handled as Customers/Suppliers — and Tally
        # system ledgers like "Profit & Loss A/c" that ERPNext derives itself).
        for l in ledgers:
            if l["_name"] in TALLY_SYSTEM_LEDGERS:
                excluded.append({
                    "name": l["_name"],
                    "reason": "Tally system ledger — ERPNext maintains this account "
                              "automatically (not imported).",
                })
                continue
            target = resolver.resolve(l["_name"])
            if target is None:
                # No classification at all — should not happen for a real ledger,
                # but if it does we record it rather than dropping it silently.
                excluded.append({
                    "name": l["_name"],
                    "reason": "Could not classify this ledger — review it in Tally.",
                })
                continue
            if target.kind != ACCOUNT:
                # A party (Customer/Supplier): migrated via extract_all, not here.
                continue
            ob, drcr = self._parse_opening(l.get("OpeningBalance", ""))
            accounts.append(AccountNode(
                name=l["_name"],
                parent=self._norm_parent(l.get("Parent", "")),
                is_group=False,
                root_type=target.root_type,
                account_type=target.account_type,
                is_reserved=False,
                opening_balance=ob,
                opening_dr_cr=drcr,
            ))

        centres = [
            CostCentreNode(name=c["_name"], parent=c.get("Parent", "").strip())
            for c in cost_centres
        ]
        return ExtractedCOA(accounts=accounts, cost_centres=centres, excluded=excluded)

    @staticmethod
    def _norm_parent(parent) -> str:
        """Tally's top-level sentinel parent "Primary" → "" (no parent)."""
        p = str(parent or "").strip()
        return "" if p == TALLY_ROOT_PARENT else p

    @staticmethod
    def _parse_opening(raw) -> tuple[float, str]:
        """Parse a Tally opening balance → (abs_amount, 'Dr'|'Cr'|'').

        Handles multi-currency cells like '10.00$ = 800.00' (takes the base amount
        after '='). Sign convention: positive = Dr, negative = Cr.
        """
        s = str(raw or "").strip()
        if not s:
            return 0.0, ""
        if "=" in s:
            s = s.split("=")[-1]
        # Tally also suffixes Dr/Cr in some exports, e.g. "15000.00 Dr".
        suffix = ""
        upper = s.upper()
        if upper.endswith("DR"):
            suffix, s = "Dr", s[:-2]
        elif upper.endswith("CR"):
            suffix, s = "Cr", s[:-2]
        s = s.replace("$", "").replace(",", "").strip()
        try:
            val = float(s)
        except ValueError:
            return 0.0, ""
        if val == 0:
            return 0.0, ""
        if suffix:
            return abs(val), suffix
        return abs(val), "Dr" if val > 0 else "Cr"
