"""The address PIN-salvage retry must re-derive the name, not reuse it.

Run via ``bench run-tests`` (imports ``tally_migrator.erpnext.importers.party``,
which imports ``frappe``); the DB is fully mocked, so no site is touched.

Why this matters: when an address insert fails on a pincode/state mismatch, the
importer drops the PIN and retries. The first (failed) attempt already ran autoname,
which - for a colliding address title - bumps a ``<title>-<type>-.#`` naming-series
counter, and then rolled back with its savepoint. But the doc kept the name it
claimed, because ``Document.set_new_name`` short-circuits while ``flags.name_set`` is
True. Reusing that name commits a value one ahead of the rolled-back counter, leaving
the series permanently behind - so the NEXT same-titled address regenerates the same
number and hits a duplicate-key error (the address is lost). The retry must therefore
clear ``name`` + ``name_set`` so autoname runs again and the counter stays in step.
"""
import contextlib
import types
import unittest
from unittest import mock

from tally_migrator.erpnext.importers import party
from tally_migrator.erpnext.importers.base import ImportResult


@contextlib.contextmanager
def _noop_atomic():
    yield


class _FakeAddr:
    """Stand-in for a frappe Address doc that fails its first insert on the pincode."""

    def __init__(self):
        self.flags = types.SimpleNamespace(name_set=False)
        self.name = None
        self.pincode = ""
        self.inserts = 0
        # (name, name_set) observed at the start of each insert attempt.
        self.states_at_insert = []

    def append(self, *args, **kwargs):
        pass

    def insert(self, **kwargs):
        self.inserts += 1
        self.states_at_insert.append((self.name, self.flags.name_set))
        if self.inserts == 1:
            # frappe's set_new_name claims a name and marks it set; then the DB insert
            # rejects the pincode/state mismatch.
            self.name = "Manpreet Singh-Billing-13"
            self.flags.name_set = True
            raise Exception("Postal Code 110001 is not associated with Punjab")
        # Second attempt: a re-derived name commits cleanly.
        self.name = "Manpreet Singh-Billing-13"


class TestAddressRetryRederivesName(unittest.TestCase):
    def test_pin_retry_clears_name_so_autoname_reruns(self):
        addr = _FakeAddr()
        imp = party.PartyImporter.__new__(party.PartyImporter)  # skip __init__/frappe
        result = ImportResult("Supplier")
        data = {"Address": "123 Street", "PinCode": "110001",
                "CountryName": "India", "MailingName": "Manpreet Singh"}

        with mock.patch.object(party, "frappe") as fr, \
                mock.patch.object(party, "atomic", _noop_atomic), \
                mock.patch.object(party, "validate_email_address", return_value=""), \
                mock.patch.object(party, "validate_gstin", return_value=(False, "")), \
                mock.patch.object(party, "_address_requires_state", return_value=True), \
                mock.patch.object(party.PartyImporter, "_resolve_state",
                                  return_value="Punjab"):
            fr.new_doc.return_value = addr
            name = imp._save_address("Manpreet Singh", "Supplier", data, result)

        # It retried exactly once after the postal-code failure.
        self.assertEqual(addr.inserts, 2)
        # The crux: at the retry, name was cleared and name_set reset, so autoname
        # re-runs and the series counter is bumped back in step with what commits.
        self.assertEqual(addr.states_at_insert[1], (None, False))
        # The salvaged address is kept and its name returned (not a hard drop).
        self.assertEqual(name, "Manpreet Singh-Billing-13")
        self.assertTrue(any("without its PIN" in w["reason"] for w in result.warnings))


if __name__ == "__main__":
    unittest.main()
