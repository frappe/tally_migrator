"""Shared ERPNext name transforms.

Single authority so that validation (collision detection) and import compute the
*same* item_code - otherwise the ITEM_CODE_COLLISION check would lie about what
the importer is actually going to create.
"""


def safe_item_code(name: str) -> str:
    """ERPNext item_code caps at 140 chars and dislikes '/'."""
    return (name or "")[:140].replace("/", "-").strip()


def company_scoped(base: str, abbr: str) -> str:
    """ERPNext names company-scoped doctypes (Account, Warehouse, Cost Center) as
    ``<base> - <ABBR>``. Returns just ``<base>`` when no abbreviation is known.
    """
    base = (base or "").strip()
    abbr = (abbr or "").strip()
    return f"{base} - {abbr}" if abbr else base
