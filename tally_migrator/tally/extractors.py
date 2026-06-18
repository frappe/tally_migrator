import unicodedata
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
    # Ledger currency name. NOTE: a real Tally masters export does NOT emit this tag
    # on the party ledger - the forex signal is the currency symbol embedded in the
    # OpeningBalance string ("-$8500 @ ... = ..."), resolved to an ISO in
    # _attach_currency_iso. Kept in the FETCH list for the live-connector path and
    # older exports that may carry it.
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
    # Batch/expiry flags (→ Item.has_batch_no / has_expiry_date). The flat tag names
    # equal FIELD.upper() (ISBATCHWISEON / ISPERISHABLEON / HASMFGDATE), so no tag_map.
    "IsBatchWiseOn", "IsPerishableOn", "HasMfgDate",
    # Maximum Retail Price (→ an "MRP" selling Price List + Item Price). Nested under
    # MRPDETAILS.LIST; the rate is unit-suffixed ("50000.00/Nos"), so it needs a path.
    "Mrp",
]

GODOWN_FIELDS     = ["Name", "Parent", "Address"]
# IsRevenue / IsDeemedPositive are Tally's own group nature flags (tags ISREVENUE /
# ISDEEMEDPOSITIVE); the resolver derives a root_type from them for custom groups
# with no reserved ancestor (see LedgerResolver.group_nature).
GROUP_FIELDS      = ["Name", "Parent", "IsRevenue", "IsDeemedPositive"]
COSTCENTRE_FIELDS = ["Name", "Parent"]
STOCKGROUP_FIELDS = ["Name", "Parent"]
UNIT_FIELDS       = [
    "Name", "IsSimpleUnit", "OriginalName", "DecimalPlaces",
    "BaseUnits", "AdditionalUnits", "Conversion", "ReportingUQC",
]
# The GST Unit Quantity Code lives in a dated revision list
# (REPORTINGUQCDETAILS.LIST/REPORTINGUQCNAME, e.g. "NOS-NUMBERS"); _resolve_field
# returns the last/most-recent entry. "Not Applicable" rows are filtered downstream.
UNIT_TAGS = {
    "ReportingUQC": ["REPORTINGUQCDETAILS.LIST/REPORTINGUQCNAME"],
}


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
    # MRP lives nested as a (state-wise, dated) revision list; take the latest rate.
    # State-specific MRP is not modelled in ERPNext, so the "Any"/last rate is used.
    "Mrp": ["MRPDETAILS.LIST/MRPRATEDETAILS.LIST/MRPRATE"],
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
    amount: float          # absolute amount in COMPANY (base) currency
    dr_cr: str             # "Dr" | "Cr" | ""
    is_advance: bool       # Tally's ISADVANCE flag
    # Absolute amount in the bill's FOREIGN currency, parsed from a forex-shaped bill
    # opening ("$600 @ 83/$ = ₹49800"); 0.0 for a plain base-currency bill. Lets the
    # importer split a foreign-currency party's opening bill-by-bill (mirroring the
    # base-currency path) only when the export actually carries per-bill foreign
    # amounts - never guessed.
    foreign_amount: float = 0.0


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
        # Enrich each party ledger with its address-book + extra phone contacts
        # (repeating child lists a real export carries beyond the single primary
        # mailing address / mobile). No-op for a live client without child lists.
        self._attach_party_subrecords(ledgers)
        # Resolve each ledger's currency symbol to an ISO code (from the CURRENCY
        # masters), so a forex party opening can post in its real currency.
        self._attach_currency_iso(ledgers)
        masters = ExtractedMasters(
            customers    = [l for l in ledgers if resolver.kind_of(l["_name"]) == CUSTOMER],
            suppliers    = [l for l in ledgers if resolver.kind_of(l["_name"]) == SUPPLIER],
            items        = self._dedup_by_name(
                self.client.get_collection("Stock Item", ITEM_FIELDS, ITEM_TAGS)),
            warehouses   = self._dedup_by_name(
                self.client.get_collection("Godown", GODOWN_FIELDS, GODOWN_TAGS)),
            stock_groups = self._dedup_by_name(
                self.client.get_collection("Stock Group", STOCKGROUP_FIELDS)),
            units        = self._dedup_by_name(
                self.client.get_collection("Unit", UNIT_FIELDS, UNIT_TAGS)),
        )
        # Attach each item's combined GST rate (IGST), read per duty head from the
        # nested rate list. No-op for a live client that can't supply it.
        self._attach_item_gst_rates(masters.items)
        # Attach each item's price levels (Retail/Wholesale rates + discounts).
        self._attach_item_price_levels(masters.items)
        # Attach each item's bills of materials (component lists).
        self._attach_item_boms(masters.items)
        # Attach each item's godown-wise opening stock (BATCHALLOCATIONS), so opening
        # stock can post per warehouse instead of collapsing into one default godown.
        self._attach_item_godown_openings(masters.items)
        return masters

    def _attach_item_gst_rates(self, items: list[dict]) -> None:
        """Set ``GstRate`` on each item dict from the source's per-duty-head rate
        read. Degrades to a no-op when the source can't supply it (live client)."""
        if not hasattr(self.client, "item_gst_rates"):
            return
        rates = self.client.item_gst_rates()
        for it in items:
            it.setdefault("GstRate", rates.get(it.get("_name", ""), ""))

    def _attach_item_price_levels(self, items: list[dict]) -> None:
        """Set ``PriceLevels`` (list of {level, date, rate, discount, ending}) on
        each item dict. No-op when the source can't supply it (live client)."""
        if not hasattr(self.client, "item_price_levels"):
            return
        levels = self.client.item_price_levels()
        for it in items:
            it.setdefault("PriceLevels", levels.get(it.get("_name", ""), []))

    def _attach_currency_iso(self, ledgers: list[dict]) -> None:
        """Set ``CurrencyISO`` on each forex ledger, resolved from the currency symbol
        embedded in its OpeningBalance (e.g. the '$' in "-$8500 @ ... = -₹705500")
        via the CURRENCY masters. Tally does NOT export a CurrencyName tag on the
        party ledger, so the symbol in the opening string is the only in-file signal.
        Only set when a foreign symbol resolves to an ISO, so domestic parties (no
        symbol, or the base ₹ which carries no ISO) are left untouched. No-op when
        the source can't supply the currency map."""
        if not hasattr(self.client, "currency_iso_map"):
            return
        iso = self.client.currency_iso_map()
        if not iso:
            return
        for r in ledgers:
            _f, symbol, _b, _d = self._parse_forex_opening(r.get("OpeningBalance", ""))
            if symbol and symbol in iso:
                r.setdefault("CurrencyISO", iso[symbol])

    def _attach_item_boms(self, items: list[dict]) -> None:
        """Set ``Boms`` (list of {name, basic_qty, components}) on each item dict.
        No-op when the source can't supply it (live client)."""
        if not hasattr(self.client, "item_boms"):
            return
        boms = self.client.item_boms()
        for it in items:
            it.setdefault("Boms", boms.get(it.get("_name", ""), []))

    def _attach_item_godown_openings(self, items: list[dict]) -> None:
        """Set ``GodownOpenings`` (list of {godown, qty, rate, value, batch, mfg_date,
        expiry}) on each item dict. ``batch``/``mfg_date``/``expiry`` are populated only
        for batch-tracked items. No-op when the source can't supply it (live client)."""
        if not hasattr(self.client, "item_godown_openings"):
            return
        godowns = self.client.item_godown_openings()
        for it in items:
            it.setdefault("GodownOpenings", godowns.get(it.get("_name", ""), []))

    @staticmethod
    def _dedup_by_name(records: list[dict]) -> list[dict]:
        """Collapse Tally's repeated master emissions into one record per name.

        Tally re-emits a master wherever it is referenced, so an export routinely
        carries the same Unit / Godown / Item several times (e.g. "Nos" twice). A
        Tally master is keyed by name, so same name = same record - not a collision.
        Left in, these phantom duplicates fire a false "will merge into one" warning
        in the preview and cause redundant import skips. Merge fields across emissions
        (preferring non-empty values) so a bare reference-stub never overwrites the
        full definition. Case-sensitive and order-stable: genuine case/whitespace
        variants (a real ERPNext merge) survive and are still flagged downstream."""
        merged: dict[str, dict] = {}
        order: list[str] = []
        for r in records:
            name = (r.get("_name") or "").strip()
            if not name:
                continue
            if name not in merged:
                merged[name] = dict(r)
                order.append(name)
                continue
            for k, v in r.items():
                if v not in (None, "") and not merged[name].get(k):
                    merged[name][k] = v
        return [merged[n] for n in order]

    def _attach_party_subrecords(self, ledgers: list[dict]) -> None:
        """Attach each ledger's extra addresses + phone contacts in place.

        A real Tally export keeps a party's address book in repeating
        ``LEDMULTIADDRESSLIST.LIST`` rows (each an ``ADDRESS.LIST/ADDRESS`` plus an
        ``ADDRESSNAME`` label) and its additional named phone numbers in
        ``CONTACTDETAILS.LIST`` (NAME / PHONENUMBER / ISDEFAULTWHATSAPPNUM). These
        are beyond the single primary mailing address + mobile the flat fields
        carry, so the importer can create extra ERPNext Address / Contact rows.
        No-op when the source can't supply child lists (live get_collection-only
        client) - the party still imports with its primary address/contact.
        """
        getter = getattr(self.client, "get_child_list", None)
        if getter is None:
            return
        addrs: dict[str, list] = {}
        for r in getter("Ledger", "LEDMULTIADDRESSLIST.LIST",
                        ["ADDRESS.LIST/ADDRESS", "ADDRESSNAME", "STATE", "PINCODE"]):
            text = (r.get("ADDRESS.LIST/ADDRESS") or "").strip()
            if not text:
                continue
            # State/pincode are usually absent on a Tally address-book row (only the
            # primary mailing address carries them), but read them when present so the
            # importer can use the row's OWN location before falling back to the party's.
            addrs.setdefault((r.get("_parent") or "").strip(), []).append({
                "address": text,
                "name": (r.get("ADDRESSNAME") or "").strip(),
                "state": (r.get("STATE") or "").strip(),
                "pincode": (r.get("PINCODE") or "").strip(),
            })
        contacts: dict[str, list] = {}
        for r in getter("Ledger", "CONTACTDETAILS.LIST",
                        ["Name", "PhoneNumber", "IsDefaultWhatsAppNum"]):
            phone = (r.get("PhoneNumber") or "").strip()
            if not phone:
                continue
            contacts.setdefault((r.get("_parent") or "").strip(), []).append({
                "name": (r.get("Name") or "").strip(),
                "phone": phone,
                "whatsapp": (r.get("IsDefaultWhatsAppNum") or "").strip().lower() == "yes",
            })
        for led in ledgers:
            nm = led.get("_name", "")
            if nm in addrs:
                led["_extra_addresses"] = addrs[nm]
            if nm in contacts:
                led["_extra_contacts"] = contacts[nm]

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
                    "reason": "ERPNext maintains this account on its own, so it was not "
                              "imported. Nothing for you to do.",
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
        # Strip currency symbols (any Unicode currency glyph, e.g. "$", "₹") plus
        # thousands commas and spaces, but KEEP letters so a unit-suffixed *quantity*
        # like "55 Nos" still fails to parse here (quantities are read by
        # _parse_quantity, not as amounts). A forex cell like
        # "-$8500.00 @ ₹ 83/$ = -₹ 705500.00" reduces (after the '=' split above) to
        # its base "-705500.00" instead of failing on the ₹ symbol.
        s = "".join(ch for ch in s
                    if ch not in (",", " ") and unicodedata.category(ch) != "Sc")
        try:
            val = float(s)
        except ValueError:
            return 0.0, ""
        if val == 0:
            return 0.0, ""
        if suffix:
            return abs(val), suffix
        return abs(val), "Dr" if val < 0 else "Cr"

    @staticmethod
    def _parse_forex_opening(raw) -> tuple[float, str, float, str]:
        """Parse a multi-currency opening into (foreign_amount, symbol, base, drcr).

        Handles the rich export form ``-$8500.00 @ ₹ 83/$ = -₹ 705500.00`` and the
        older ``10.00$ = 800.00``: the foreign amount/symbol come from the part
        before ``@`` (or before ``=``), the base from after ``=``. Sign convention
        matches _parse_opening (negative = Dr). Returns zeros when there is no ``=``
        (not a forex cell) or nothing parses."""
        s = str(raw or "").strip()
        if "=" not in s:
            return 0.0, "", 0.0, ""
        left, _, right = s.partition("=")
        foreign_part = left.split("@")[0]
        symbol = "".join(ch for ch in foreign_part
                         if not (ch.isdigit() or ch in " .,-")).strip()

        def _num(x):
            x = re.sub(r"[^\d.\-]", "", x)
            try:
                return float(x)
            except ValueError:
                return 0.0
        foreign, base = _num(foreign_part), _num(right)
        drcr = "Dr" if (foreign < 0 or base < 0) else "Cr"
        return abs(foreign), symbol, abs(base), drcr

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
            raw = r.get("OpeningBalance", "")
            amount, drcr = self._parse_opening(raw)
            if not amount:
                continue
            # A forex-shaped bill ("$600 @ 83/$ = ₹49800") yields a foreign amount too;
            # ``amount`` above is already the base (the part after '='), so the two are
            # consistent. A plain base-currency bill has no '='/symbol → foreign 0.
            foreign, _sym, _base, _fdrcr = self._parse_forex_opening(raw)
            bills.append(BillAllocation(
                party=(r.get("_parent") or "").strip(),
                bill_no=(r.get("Name") or "").strip(),
                bill_date=self._parse_tally_date(r.get("BillDate", "")),
                amount=amount,
                dr_cr=drcr,
                is_advance=(r.get("IsAdvance") or "").strip().lower() == "yes",
                foreign_amount=foreign,
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
