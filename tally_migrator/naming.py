"""Shared ERPNext name transforms.

Single authority so that validation (collision detection) and import compute the
*same* item_code — otherwise the ITEM_CODE_COLLISION check would lie about what
the importer is actually going to create.
"""


def safe_item_code(name: str) -> str:
    """ERPNext item_code caps at 140 chars and dislikes '/'."""
    return (name or "")[:140].replace("/", "-").strip()
