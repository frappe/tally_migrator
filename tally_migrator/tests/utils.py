"""
Shared helpers for tally_migrator integration tests.

Integration tests must not depend on ambient site data. They either provision
what they need or skip cleanly when an environment prerequisite (a configured
ERPNext Company) is missing.
"""
import unittest

import frappe

TEST_PREFIX = "_TMTest"

# (doctype, field that holds the human name) — order matters for FK-safe deletes.
_CLEANUP_TARGETS = [
    ("Address", "address_title"),
    ("Customer", "customer_name"),
    ("Supplier", "supplier_name"),
    ("Item", "item_code"),
    ("Warehouse", "warehouse_name"),
    ("Item Group", "item_group_name"),
]


def get_company() -> str | None:
    """Return an existing Company name, or None if the site has none."""
    companies = frappe.get_all("Company", pluck="name", limit=1)
    return companies[0] if companies else None


def require_company() -> str:
    """Return a Company name, or skip the test when none is configured."""
    company = get_company()
    if not company:
        raise unittest.SkipTest("No ERPNext Company is configured on this site")
    return company


def cleanup_test_records() -> None:
    """
    Delete any records left by previous runs (the importer commits per record,
    so a plain rollback cannot undo them). Idempotent and best-effort.
    """
    for doctype, field in _CLEANUP_TARGETS:
        for name in frappe.get_all(doctype, filters={field: ["like", f"{TEST_PREFIX}%"]}, pluck="name"):
            try:
                frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
            except Exception:
                pass
    frappe.db.commit()
