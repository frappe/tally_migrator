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
