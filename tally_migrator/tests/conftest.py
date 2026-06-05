"""
Pytest configuration for tally_migrator tests.

Run all tests:
    bench --site <sitename> run-tests --app tally_migrator

Run a specific module:
    bench --site <sitename> run-tests --module tally_migrator.tests.test_extractor

Run a specific DocType:
    bench --site <sitename> run-tests --doctype "Tally Migration Log"
"""
import frappe
import pytest


@pytest.fixture(autouse=True)
def reset_db():
    """Roll back every test so they stay isolated."""
    yield
    frappe.db.rollback()


@pytest.fixture(scope="session")
def admin_user():
    frappe.set_user("Administrator")
    return "Administrator"
