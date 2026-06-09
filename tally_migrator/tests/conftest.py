"""
Pytest configuration for tally_migrator tests.

Run all tests:
    bench --site <sitename> run-tests --app tally_migrator

Run a specific module:
    bench --site <sitename> run-tests --module tally_migrator.tests.test_extractor

Run a specific DocType:
    bench --site <sitename> run-tests --doctype "Tally Migration Log"

Two tiers of tests
------------------
Most modules are pure (no Frappe) and run under a plain interpreter:
    python3 -m unittest discover -s tally_migrator/tests -p 'test_*.py'

Four modules import ``frappe`` and therefore only run under ``bench``:
``test_importer``, ``test_file_source``, ``test_critical_fixes``, ``test_summary``.
Under a bare ``python3`` they error with ``ModuleNotFoundError: No module named
'frappe'`` - that is expected, not a regression. CI MUST run the full suite via
``bench run-tests`` so those paths (importers, streaming parser, IDOR guard,
opening-balance idempotency) are actually exercised; the pure run alone leaves
them untested.
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
