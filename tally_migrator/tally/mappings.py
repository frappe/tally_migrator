from __future__ import annotations

# ── Tally root groups that classify ledgers as customers / suppliers ──────────

DEBTOR_ROOTS   = {"Sundry Debtors"}
CREDITOR_ROOTS = {"Sundry Creditors"}

# ── Unit of Measure: Tally → ERPNext ─────────────────────────────────────────

UOM_MAP: dict[str, str] = {
    # Count
    "Nos": "Nos", "No": "Nos", "NOS": "Nos", "PCS": "Nos", "Pcs": "Nos",
    "Units": "Nos", "Unit": "Nos", "U": "Nos", "EA": "Nos", "Each": "Nos",
    "Pc": "Nos", "pc": "Nos",
    # Weight
    "Kgs": "Kg", "KG": "Kg", "Kg": "Kg", "KGS": "Kg", "kgs": "Kg",
    "Gm": "Gram", "GM": "Gram", "GMS": "Gram", "g": "Gram", "Gms": "Gram",
    "Ton": "Tonne", "TON": "Tonne", "MT": "Tonne", "Tonne": "Tonne",
    "Quintal": "Quintal", "QTL": "Quintal",
    # Volume
    "Ltr": "Litre", "LTR": "Litre", "Lts": "Litre", "L": "Litre", "ltr": "Litre",
    "Ml": "Ml", "ML": "Ml", "ml": "Ml",
    # Length
    "Mtr": "Metre", "MTR": "Metre", "Meter": "Metre", "M": "Metre", "mtr": "Metre",
    "Feet": "Feet", "FT": "Feet", "Ft": "Feet", "ft": "Feet",
    "Inch": "Inch", "INCH": "Inch", "IN": "Inch", "in": "Inch",
    "Cm": "Cm", "CM": "Cm", "cm": "Cm",
    "Mm": "Mm", "MM": "Mm",
    # Area
    "Sqft": "Sq ft", "SQFT": "Sq ft", "SqFt": "Sq ft", "sq ft": "Sq ft",
    "Sqm": "Sq m", "SQM": "Sq m",
    # Packs
    "Box": "Box", "BOX": "Box",
    "Doz": "Dozen", "DOZ": "Dozen", "Dozen": "Dozen",
    "Pkt": "Packet", "PKT": "Packet", "Packet": "Packet",
    "Roll": "Roll", "ROLL": "Roll",
    "Set": "Set", "SET": "Set",
    "Bag": "Bag", "BAG": "Bag",
    "Bndl": "Bundle", "Bundle": "Bundle",
    "Can": "Can", "CAN": "Can",
    "Pair": "Pair", "PAIR": "Pair",
    "Sheet": "Sheet", "SHEET": "Sheet",
    # Time
    "Hrs": "Hour", "HRS": "Hour", "Hr": "Hour", "Hour": "Hour",
    "Day": "Day", "DAY": "Day",
    "Wk": "Week", "WK": "Week", "Week": "Week",
    "Month": "Month", "MONTH": "Month",
}

DEFAULT_UOM = "Nos"

# ── Tally state names → ERPNext state names ───────────────────────────────────

TALLY_STATE_MAP: dict[str, str] = {
    "Andaman and Nicobar Islands": "Andaman and Nicobar Islands",
    "Andhra Pradesh":              "Andhra Pradesh",
    "Arunachal Pradesh":           "Arunachal Pradesh",
    "Assam":                       "Assam",
    "Bihar":                       "Bihar",
    "Chandigarh":                  "Chandigarh",
    "Chhattisgarh":                "Chhattisgarh",
    "Dadra and Nagar Haveli":      "Dadra and Nagar Haveli",
    "Daman and Diu":               "Daman and Diu",
    "Delhi":                       "Delhi",
    "Goa":                         "Goa",
    "Gujarat":                     "Gujarat",
    "Haryana":                     "Haryana",
    "Himachal Pradesh":            "Himachal Pradesh",
    "Jammu & Kashmir":             "Jammu and Kashmir",
    "Jammu and Kashmir":           "Jammu and Kashmir",
    "Jharkhand":                   "Jharkhand",
    "Karnataka":                   "Karnataka",
    "Kerala":                      "Kerala",
    "Ladakh":                      "Ladakh",
    "Lakshadweep":                 "Lakshadweep",
    "Madhya Pradesh":              "Madhya Pradesh",
    "Maharashtra":                 "Maharashtra",
    "Manipur":                     "Manipur",
    "Meghalaya":                   "Meghalaya",
    "Mizoram":                     "Mizoram",
    "Nagaland":                    "Nagaland",
    "Odisha":                      "Odisha",
    "Puducherry":                  "Puducherry",
    "Punjab":                      "Punjab",
    "Rajasthan":                   "Rajasthan",
    "Sikkim":                      "Sikkim",
    "Tamil Nadu":                  "Tamil Nadu",
    "Telangana":                   "Telangana",
    "Tripura":                     "Tripura",
    "Uttar Pradesh":               "Uttar Pradesh",
    "Uttarakhand":                 "Uttarakhand",
    "West Bengal":                 "West Bengal",
}

# ── ERPNext defaults ──────────────────────────────────────────────────────────
# Customer Group MUST be a non-group (leaf) node — ERPNext rejects assigning a
# group node ("All Customer Groups") to a Customer. "Commercial" is a standard
# ERPNext leaf group present on every install.
DEFAULT_CUSTOMER_GROUP = "Commercial"
DEFAULT_SUPPLIER_GROUP = "All Supplier Groups"
DEFAULT_ITEM_GROUP     = "All Item Groups"
DEFAULT_TERRITORY      = "All Territories"
# Base name of ERPNext's root warehouse. Warehouses are company-scoped and
# suffixed with the company abbreviation, e.g. "All Warehouses - ABC".
DEFAULT_WAREHOUSE      = "All Warehouses"


# ── Chart of Accounts: Tally group → ERPNext account classification ───────────
#
# Tally ships ~28 reserved "primary" groups whose meaning is fixed. Every other
# group/ledger inherits its nature from the nearest reserved ancestor. Each entry
# maps a reserved Tally group to:
#   root          — ERPNext root_type (Asset/Liability/Income/Expense/Equity)
#   account_type  — ERPNext account_type ("" = ordinary group/ledger)
#   erpnext_group — the ERPNext standard-COA group to reuse in coa_mode="reuse"

ASSET, LIABILITY, INCOME, EXPENSE, EQUITY = (
    "Asset", "Liability", "Income", "Expense", "Equity",
)

TALLY_GROUP_CLASSIFICATION: dict[str, dict] = {
    # Equity
    "Capital Account":          {"root": EQUITY,    "account_type": "Equity",             "erpnext_group": "Capital Account"},
    "Reserves & Surplus":       {"root": EQUITY,    "account_type": "Equity",             "erpnext_group": "Capital Account"},
    "Retained Earnings":        {"root": EQUITY,    "account_type": "Equity",             "erpnext_group": "Capital Account"},
    # Liabilities
    "Loans (Liability)":        {"root": LIABILITY, "account_type": "",                   "erpnext_group": "Loans (Liability)"},
    "Secured Loans":            {"root": LIABILITY, "account_type": "",                   "erpnext_group": "Loans (Liability)"},
    "Unsecured Loans":          {"root": LIABILITY, "account_type": "",                   "erpnext_group": "Loans (Liability)"},
    "Bank OD A/c":              {"root": LIABILITY, "account_type": "Bank",               "erpnext_group": "Loans (Liability)"},
    "Bank OCC A/c":             {"root": LIABILITY, "account_type": "Bank",               "erpnext_group": "Loans (Liability)"},
    "Current Liabilities":      {"root": LIABILITY, "account_type": "",                   "erpnext_group": "Current Liabilities"},
    "Duties & Taxes":           {"root": LIABILITY, "account_type": "Tax",                "erpnext_group": "Duties and Taxes"},
    "Provisions":               {"root": LIABILITY, "account_type": "",                   "erpnext_group": "Current Liabilities"},
    "Sundry Creditors":         {"root": LIABILITY, "account_type": "Payable",            "erpnext_group": "Current Liabilities"},
    "Branch / Divisions":       {"root": LIABILITY, "account_type": "",                   "erpnext_group": "Current Liabilities"},
    "Suspense A/c":             {"root": LIABILITY, "account_type": "Temporary",          "erpnext_group": "Current Liabilities"},
    # Assets
    "Fixed Assets":             {"root": ASSET,     "account_type": "Fixed Asset",        "erpnext_group": "Fixed Assets"},
    "Investments":              {"root": ASSET,     "account_type": "",                   "erpnext_group": "Investments"},
    "Current Assets":           {"root": ASSET,     "account_type": "",                   "erpnext_group": "Current Assets"},
    "Bank Accounts":            {"root": ASSET,     "account_type": "Bank",               "erpnext_group": "Bank Accounts"},
    "Cash-in-Hand":             {"root": ASSET,     "account_type": "Cash",               "erpnext_group": "Cash In Hand"},
    "Deposits (Asset)":         {"root": ASSET,     "account_type": "",                   "erpnext_group": "Current Assets"},
    "Loans & Advances (Asset)": {"root": ASSET,     "account_type": "",                   "erpnext_group": "Current Assets"},
    "Stock-in-Hand":            {"root": ASSET,     "account_type": "Stock",              "erpnext_group": "Stock Assets"},
    "Sundry Debtors":           {"root": ASSET,     "account_type": "Receivable",         "erpnext_group": "Current Assets"},
    "Misc. Expenses (ASSET)":   {"root": ASSET,     "account_type": "",                   "erpnext_group": "Current Assets"},
    # Income
    "Sales Accounts":           {"root": INCOME,    "account_type": "Income Account",     "erpnext_group": "Direct Income"},
    "Direct Incomes":           {"root": INCOME,    "account_type": "Income Account",     "erpnext_group": "Direct Income"},
    "Indirect Incomes":         {"root": INCOME,    "account_type": "Income Account",     "erpnext_group": "Indirect Income"},
    # Expenses
    "Purchase Accounts":        {"root": EXPENSE,   "account_type": "Cost of Goods Sold", "erpnext_group": "Direct Expenses"},
    "Direct Expenses":          {"root": EXPENSE,   "account_type": "Expense Account",    "erpnext_group": "Direct Expenses"},
    "Indirect Expenses":        {"root": EXPENSE,   "account_type": "Expense Account",    "erpnext_group": "Indirect Expenses"},
}

# Tally display-name variants → canonical reserved-group key above.
TALLY_GROUP_ALIASES: dict[str, str] = {
    "Income (Direct)":        "Direct Incomes",
    "Direct Income":          "Direct Incomes",
    "Income (Indirect)":      "Indirect Incomes",
    "Indirect Income":        "Indirect Incomes",
    "Expenses (Direct)":      "Direct Expenses",
    "Direct Expense":         "Direct Expenses",
    "Expenses (Indirect)":    "Indirect Expenses",
    "Indirect Expense":       "Indirect Expenses",
    "Duties and Taxes":       "Duties & Taxes",
    "Cash-in-hand":           "Cash-in-Hand",
    "Misc. Expenses (Asset)": "Misc. Expenses (ASSET)",
}

# Tally groups whose ledgers are migrated as PARTIES (Customer/Supplier), not as
# ledger Accounts. Their descendants are resolved at runtime from the group tree.
PARTY_ROOT_GROUPS = DEBTOR_ROOTS | CREDITOR_ROOTS

# Tally marks every top-level (primary) group/ledger with the sentinel parent
# "Primary". It has no ERPNext equivalent — such nodes attach directly under the
# relevant root group — so it must be normalised to "" (no parent) on extraction.
TALLY_ROOT_PARENT = "Primary"

# Tally system ledgers ERPNext derives itself (it computes its own P&L) — never
# migrated as ledger Accounts.
TALLY_SYSTEM_LEDGERS = {"Profit & Loss A/c"}

# ERPNext's standard root-type representative groups, used as a last-resort parent
# in coa_mode="reuse" when a more specific default group can't be found.
ERPNEXT_ROOT_GROUPS: dict[str, str] = {
    ASSET:     "Application of Funds (Assets)",
    LIABILITY: "Source of Funds (Liabilities)",
    INCOME:    "Income",
    EXPENSE:   "Expenses",
    EQUITY:    "Equity",
}


def classify_group(name: str) -> dict | None:
    """Return the classification for a reserved Tally group, else None.

    Accepts display-name aliases. ``None`` means the group is user-defined and its
    nature must be inherited from its nearest reserved ancestor.
    """
    key = TALLY_GROUP_ALIASES.get(name, name)
    return TALLY_GROUP_CLASSIFICATION.get(key)
