import re
from dataclasses import dataclass, field
from .mappings import TALLY_ROOT_PARENT, TALLY_SYSTEM_LEDGERS, classify_group
from .resolver import ACCOUNT, CUSTOMER, SUPPLIER, LedgerResolver


# ── Field lists sent to Tally via TDL FETCH ───────────────────────────────────

LEDGER_FIELDS = [
    "Name", "Parent", "Address", "GSTRegistrationNumber",
    "INCOMETAXNumber", "OpeningBalance", "BillCreditPeriod",
    "LedgerPhone", "LedgerMobile", "LedgerEmail",
    "CountryName", "LedgerState", "PinCode",
    # P1 standard fields Tally states explicitly on the party ledger.
    "GSTRegistrationType", "CreditLimit", "EmailCC", "LedgerContact", "MailingName",
    # Ledger currency name. Almost always the company base currency (redundant), but
    # a forex party ledger carries a different one - the only in-file signal that a
    # party's opening must NOT be posted in the company currency (see
    # PartyOpeningImporter). Compared by equality, never mapped to an ISO code.
    "CurrencyName",
    # Bank account details (→ ERPNext Bank Account, linked to the party).
    "BankAccountNo", "BankIFSC", "BankAccountHolder", "BankBranch", "BankName",
]

ITEM_FIELDS = [
    "Name", "Parent", "BaseUnits", "StandardCost", "StandardPrice",
    "OpeningBalance", "OpeningRate", "OpeningValue", "Description",
    "HSNCode", "GSTTaxability", "TypeOfSupply",
    # Inventory valuation method (→ Item.valuation_method) and the flat item-level
    # GST flag (→ India-Compliance is_non_gst). Both have real ERPNext targets.
    "ValuationMethod", "GstApplicable",
]

GODOWN_FIELDS     = ["Name", "Parent", "Address"]
GROUP_FIELDS      = ["Name", "Parent"]
COSTCENTRE_FIELDS = ["Name", "Parent"]
STOCKGROUP_FIELDS = ["Name", "Parent"]
UNIT_FIELDS       = [
    "Name", "IsSimpleUnit", "OriginalName", "DecimalPlaces",
    "BaseUnits", "AdditionalUnits", "Conversion",
]


# ── Tag overrides for fields whose real Tally tag ≠ FIELD.upper() ─────────────
# A genuine Tally Prime "Export Masters (XML)" names several fields differently
# from the field key and wraps some in ``.LIST`` containers. These maps give the
# *exact* tag(s) the parser reads for such a field; every other field falls back
# to FIELD.upper(), which already equals the real Tally tag (NAME, PARENT, …).
# (A candidate dict with ``join`` concatenates repeated nodes, e.g. address lines.)
_ADDRESS_LIST = {"path": "ADDRESS.LIST/ADDRESS", "join": ", "}

# A real Tally Prime *export* nests a party ledger's mailing + GST details inside
# these containers; the flat tags after them are the import-schema / older-export
# fallbacks the PDF documents. The parser tries each candidate in order and takes
# the first that yields a value, so it handles both export shapes and degrades
# gracefully when a field is absent. (Confirmed against real exports.)
_MAIL = "LEDMAILINGDETAILS.LIST"      # address / state / pincode / mailing name / country
_GSTREG = "LEDGSTREGDETAILS.LIST"     # GSTIN + registration type

LEDGER_TAGS = {
    "Address":     [{"path": f"{_MAIL}/ADDRESS.LIST/ADDRESS", "join": ", "}, _ADDRESS_LIST],
    "LedgerState": [f"{_MAIL}/STATE", "LEDSTATENAME", "STATENAME"],
    "PinCode":     [f"{_MAIL}/PINCODE", "PINCODE"],
    "CountryName": [f"{_MAIL}/COUNTRY", "COUNTRYOFRESIDENCE", "COUNTRYNAME"],
    "MailingName": [f"{_MAIL}/MAILINGNAME", "MAILINGNAME.LIST/MAILINGNAME"],
    "LedgerEmail": [f"{_MAIL}/EMAIL", "EMAIL"],
    "GSTRegistrationNumber": [f"{_GSTREG}/GSTIN", "GSTREGISTRATIONNUMBER", "PARTYGSTIN"],
    "GSTRegistrationType":   [f"{_GSTREG}/GSTREGISTRATIONTYPE", "GSTREGISTRATIONTYPE"],
    "LedgerContact": ["LEDGERCONTACT", "CONTACTPERSON"],
    # Bank details. A real Tally Prime export nests the account under
    # PAYMENTDETAILS.LIST (ACCOUNTNUMBER / IFSCODE / BANKNAME); the flat tags are an
    # older/alternative shape some ledgers still use. Try the nested path first, then
    # the flat fallback, so either export shape populates the ERPNext Bank Account.
    # (Confirmed against real exports: the nested shape carried 0 bank fields through
    # before this - every party's bank account silently dropped.) A ledger with more
    # than one PAYMENTDETAILS.LIST entry yields the last; multi-bank parties are rare
    # and ERPNext stores one account here.
    "BankAccountNo":     ["PAYMENTDETAILS.LIST/ACCOUNTNUMBER", "BANKDETAILS"],
    "BankIFSC":          ["PAYMENTDETAILS.LIST/IFSCODE", "IFSCODE"],
    "BankAccountHolder": ["BANKACCHOLDERNAME"],
    "BankBranch":        ["BRANCHNAME"],
    "BankName":          ["PAYMENTDETAILS.LIST/BANKNAME", "BANKINGCONFIGBANK"],
}

ITEM_TAGS = {
    # Tally keeps standard price/cost as dated revision lists; take the latest.
    "StandardPrice": ["STANDARDPRICELIST.LIST/RATE"],
    "StandardCost":  ["STANDARDCOSTLIST.LIST/RATE"],
    # Confirmed against a real Stock Item export: the HSN code lives nested in
    # HSNDETAILS.LIST/HSNCODE (the sibling <HSN> tag holds the *description*, not
    # the code). Taxability + Goods/Services nest under GSTDETAILS.LIST. The flat
    # forms are version fallbacks. (Item GST *rate* is intentionally not read - it
    # is nested per duty-head and usually inherited via SRCOFGSTDETAILS, so the
    # item-level value is 0/unreliable; ERPNext models rate as a tax template.)
    "HSNCode":       ["HSNDETAILS.LIST/HSNCODE", "HSNCODE"],
    "GSTTaxability": ["GSTDETAILS.LIST/TAXABILITY"],
    "TypeOfSupply":  ["GSTDETAILS.LIST/SUPPLYTYPE", "TYPEOFSUPPLY", "GSTTYPEOFSUPPLY"],
    # Tally exposes the costing/market valuation under either tag; "Avg. Cost"/
    # "Avg. Price" → Moving Average, FIFO/LIFO map straight across.
    "ValuationMethod": ["VALUATIONMETHOD", "COSTINGMETHOD"],
    # Flat item-level "Applicable / Not Applicable" GST switch.
    "GstApplicable":   ["GSTAPPLICABLE"],
}

GODOWN_TAGS = {
    "Address": [_ADDRESS_LIST],
}


@dataclass
class ExtractedMasters:
    customers:    list[dict]
    suppliers:    list[dict]
    items:        list[dict]
    warehouses:   list[dict]
    # Inventory structure masters that items depend on - imported before items so
    # an item nests under its real (nested) group and uses a real UOM. Default to
    # empty so older callers/tests constructing ExtractedMasters still work.
    stock_groups: list[dict] = field(default_factory=list)
    units:        list[dict] = field(default_factory=list)

    @property
    def summary(self) -> dict:
        return {
            "customers":    len(self.customers),
            "suppliers":    len(self.suppliers),
            "items":        len(self.items),
            "warehouses":   len(self.warehouses),
            "stock_groups": len(self.stock_groups),
            "units":        len(self.units),
        }


@dataclass
class AccountNode:
    """One ERPNext Account to create - a Tally group (is_group) or ledger."""
    name: str
    parent: str            # immediate Tally parent group ("" for a primary group)
    is_group: bool
    root_type: str         # ERPNext root_type (Asset/Liability/Income/Expense/Equity)
    account_type: str      # ERPNext account_type ("" = ordinary)
    is_reserved: bool      # True for Tally's built-in primary groups
    opening_balance: float = 0.0
    opening_dr_cr: str = ""    # "Dr" | "Cr" | ""
    # Bank details (only on account_type == "Bank" ledgers) → company Bank Account.
    bank_account_no: str = ""
    bank_ifsc: str = ""
    bank_name: str = ""
    bank_holder: str = ""


@dataclass
class CostCentreNode:
    name: str
    parent: str            # Tally parent centre ("" if top level)


@dataclass
class BillAllocation:
    """One bill-wise opening reference under a party ledger.

    Tally stores a party's opening balance bill-by-bill when "Maintain balances
    bill-by-bill" is on (the default for Sundry Debtors/Creditors), and a real
    masters export carries each bill inside ``BILLALLOCATIONS.LIST`` on the
    ledger. ``amount``/``dr_cr`` use the same sign convention as the ledger
    opening (see :meth:`TallyExtractor._parse_opening`); the bills net to the
    ledger's own opening. ``is_advance`` is Tally's own flag (an opening advance
    rather than an outstanding bill). Classification into an ERPNext opening
    invoice vs an advance Payment Entry is left to the importer, which knows the
    party type (and therefore the natural side) - this stays a faithful,
    unopinionated parse of the file.
    """
    party: str             # owning ledger (the Tally party name)
    bill_no: str           # the bill reference (Tally bill NAME)
    bill_date: str         # ISO date "YYYY-MM-DD" ("" when absent/unparseable)
    amount: float          # absolute amount
    dr_cr: str             # "Dr" | "Cr" | ""
    is_advance: bool       # Tally's ISADVANCE flag


@dataclass
class ExtractedCOA:
    accounts:     list[AccountNode]
    cost_centres: list[CostCentreNode]
    # Ledgers deliberately or unavoidably left out of the COA, with a reason, so
    # nothing is dropped silently. Parties (Customers/Suppliers) are NOT listed
    # here - they are migrated through ``extract_all`` and counted there.
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
        # - currently FileTallySource (an uploaded Tally masters XML export).
        self.client = client

    def extract_all(self) -> ExtractedMasters:
        groups  = self.client.get_collection("Group", GROUP_FIELDS)
        ledgers = self.client.get_collection("Ledger", LEDGER_FIELDS, LEDGER_TAGS)

        # One resolver classifies every ledger (customer / supplier / account) by
        # its group ancestry - the single source of truth also used by COA
        # extraction, so customer/supplier splitting needs no parallel BFS here.
        resolver = LedgerResolver(groups, ledgers)
        return ExtractedMasters(
            customers    = [l for l in ledgers if resolver.kind_of(l["_name"]) == CUSTOMER],
            suppliers    = [l for l in ledgers if resolver.kind_of(l["_name"]) == SUPPLIER],
            items        = self.client.get_collection("Stock Item", ITEM_FIELDS, ITEM_TAGS),
            warehouses   = self.client.get_collection("Godown", GODOWN_FIELDS, GODOWN_TAGS),
            stock_groups = self.client.get_collection("Stock Group", STOCKGROUP_FIELDS),
            units        = self.client.get_collection("Unit", UNIT_FIELDS),
        )

    # ── Chart of Accounts ──────────────────────────────────────────────────────

    def extract_coa(self) -> ExtractedCOA:
        """Extract the full Chart of Accounts + Cost Centres.

        Ledgers under Sundry Debtors/Creditors are excluded - they migrate as
        Customers/Suppliers (handled separately), not as ledger Accounts.
        """
        groups       = self.client.get_collection("Group", GROUP_FIELDS)
        ledgers      = self.client.get_collection("Ledger", LEDGER_FIELDS, LEDGER_TAGS)
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

        # Ledger nodes (skip parties - handled as Customers/Suppliers - and Tally
        # system ledgers like "Profit & Loss A/c" that ERPNext derives itself).
        for l in ledgers:
            if l["_name"] in TALLY_SYSTEM_LEDGERS:
                excluded.append({
                    "name": l["_name"],
                    "reason": "Tally system ledger - ERPNext maintains this account "
                              "automatically (not imported).",
                })
                continue
            target = resolver.resolve(l["_name"])
            if target is None:
                # No classification at all - should not happen for a real ledger,
                # but if it does we record it rather than dropping it silently.
                excluded.append({
                    "name": l["_name"],
                    "reason": "Could not classify this ledger - review it in Tally.",
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
                bank_account_no=(l.get("BankAccountNo") or "").strip(),
                bank_ifsc=(l.get("BankIFSC") or "").strip(),
                bank_name=(l.get("BankName") or "").strip(),
                bank_holder=(l.get("BankAccountHolder") or "").strip(),
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
    def _parse_quantity(raw) -> float:
        """Parse a Tally stock opening *quantity* → float.

        Stock-item opening balances are unit-suffixed quantities like ``"55 Nos"``
        or ``"100.50 Kgs"`` (and occasionally a multi-godown ``"=`` cell), which the
        amount parser (:meth:`_parse_opening`) and the plain ``float`` path both
        read as 0. Strip the leading signed-decimal token and ignore the trailing
        unit name. Returns 0.0 when there is no leading number.
        """
        s = str(raw or "").strip()
        if not s:
            return 0.0
        if "=" in s:
            s = s.split("=")[-1].strip()
        s = s.replace(",", "")
        m = re.match(r"[-+]?\d*\.?\d+", s)
        if not m:
            return 0.0
        try:
            return float(m.group(0))
        except ValueError:
            return 0.0

    @staticmethod
    def _parse_rate(raw) -> float:
        """Parse a Tally stock *rate* → float.

        Opening rates export unit-suffixed, e.g. ``"1.00/Nos"`` or ``"12.50/Kg"``,
        which a plain ``float`` (and :meth:`BaseImporter._to_float`) read as 0. Take
        the leading signed-decimal token and ignore the trailing ``/unit``. Returns
        0.0 when there is no leading number. Rates are magnitudes, so a Tally sign
        is dropped (``abs``) - direction lives on the quantity/value, not the rate.
        """
        s = str(raw or "").strip().replace(",", "")
        if not s:
            return 0.0
        m = re.match(r"[-+]?\d*\.?\d+", s)
        if not m:
            return 0.0
        try:
            return abs(float(m.group(0)))
        except ValueError:
            return 0.0

    @staticmethod
    def _parse_opening(raw) -> tuple[float, str]:
        """Parse a Tally opening balance → (abs_amount, 'Dr'|'Cr'|'').

        Handles multi-currency cells like '10.00$ = 800.00' (takes the base amount
        after '='). Sign convention: **negative = Dr, positive = Cr**. Tally stores
        a Debit opening as a negative number and a Credit opening as positive -
        verified against real exports where the Capital Account (always a credit)
        exports positive and bank/asset balances export negative, and where Sundry
        Debtors (normally Dr) are predominantly negative while Sundry Creditors
        (normally Cr) are predominantly positive. An explicit 'Dr'/'Cr' suffix, when
        present, always wins over the bare sign.
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
        return abs(val), "Dr" if val < 0 else "Cr"

    # ── Bill-wise opening balances ─────────────────────────────────────────────
    def extract_bill_allocations(self) -> list[BillAllocation]:
        """Extract every party's bill-wise opening references from the export.

        Reads the repeating ``BILLALLOCATIONS.LIST`` under each ledger. Rows with
        no usable amount are dropped (a zero/blank bill carries no balance). The
        result is a flat list across all parties; the importer groups it by
        ``party`` and decides, per party type, which bills become opening invoices
        and which become advance Payment Entries.

        Returns ``[]`` when the source cannot supply child lists (e.g. a live
        client that only implements ``get_collection``), so callers degrade to the
        ledger-level opening with no bill detail.
        """
        getter = getattr(self.client, "get_child_list", None)
        if getter is None:
            return []
        rows = getter(
            "Ledger", "BILLALLOCATIONS.LIST",
            ["BillDate", "Name", "IsAdvance", "OpeningBalance"])
        bills: list[BillAllocation] = []
        for r in rows:
            amount, drcr = self._parse_opening(r.get("OpeningBalance", ""))
            if not amount:
                continue
            bills.append(BillAllocation(
                party=(r.get("_parent") or "").strip(),
                bill_no=(r.get("Name") or "").strip(),
                bill_date=self._parse_tally_date(r.get("BillDate", "")),
                amount=amount,
                dr_cr=drcr,
                is_advance=(r.get("IsAdvance") or "").strip().lower() == "yes",
            ))
        return bills

    @staticmethod
    def _parse_tally_date(raw) -> str:
        """Tally ``YYYYMMDD`` (e.g. ``"20200310"``) → ISO ``"2020-03-10"``.

        Returns "" for a blank or non-8-digit value rather than guessing - the
        importer then falls back to the migration posting date for that bill.
        """
        s = str(raw or "").strip()
        if not re.fullmatch(r"\d{8}", s):
            return ""
        y, m, d = s[:4], s[4:6], s[6:8]
        if not ("01" <= m <= "12" and "01" <= d <= "31"):
            return ""
        return f"{y}-{m}-{d}"
