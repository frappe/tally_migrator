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
Most modules are pure (no Frappe). Run them under a plain interpreter with:
    make test-pure            (= python3 -m tally_migrator.tests.run_pure)

That runner skips the frappe-tier modules cleanly. A naive
``python3 -m unittest discover`` instead ERRORS on them, because four modules
import ``frappe`` and cannot load outside ``bench``: ``test_importer``,
``test_file_source``, ``test_critical_fixes``, ``test_summary``. Run the FULL
suite (including those four - the importers, streaming parser, IDOR guard and
opening-balance idempotency) with:
    make test-bench           (= bench --site <site> run-tests --app tally_migrator)

CI MUST run test-bench; the pure run alone leaves those paths untested.
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
