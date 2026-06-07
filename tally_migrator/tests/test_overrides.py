"""Tests for record overrides + editable-field reporting. Pure, runs locally:
    python -m unittest tally_migrator.tests.test_overrides
"""
import unittest

from tally_migrator.tally.extractors import ExtractedMasters
from tally_migrator.migration.overrides import apply_record_overrides
from tally_migrator.validation.engine import (
    validate_masters, group_report, records_by_key, erpnext_states, EDITABLE_FIELDS,
)


def _party(name, **over):
    rec = {
        "_name": name, "GSTRegistrationNumber": "", "CountryName": "India",
        "LedgerState": "Maharashtra", "PinCode": "400001",
        "LedgerPhone": "", "LedgerMobile": "",
    }
    rec.update(over)
    return rec


def _item(name, hsn="1234"):
    return {"_name": name, "HSNCode": hsn, "BaseUnits": "Nos", "Parent": ""}


def _masters(customers=None, suppliers=None, items=None):
    return ExtractedMasters(customers=customers or [], suppliers=suppliers or [],
                            items=items or [], warehouses=[])


class TestApplyOverrides(unittest.TestCase):
    def test_patches_named_record_field(self):
        m = _masters(customers=[_party("X", LedgerState="")])
        apply_record_overrides(m, {"Customer": {"X": {"LedgerState": "Gujarat"}}})
        self.assertEqual(m.customers[0]["LedgerState"], "Gujarat")

    def test_blank_value_is_ignored(self):
        m = _masters(items=[_item("Widget", hsn="1234")])
        apply_record_overrides(m, {"Item": {"Widget": {"HSNCode": ""}}})
        self.assertEqual(m.items[0]["HSNCode"], "1234")  # unchanged

    def test_unknown_record_is_noop(self):
        m = _masters(customers=[_party("X")])
        apply_record_overrides(m, {"Customer": {"Nope": {"LedgerState": "Goa"}}})
        self.assertEqual(m.customers[0]["LedgerState"], "Maharashtra")

    def test_empty_overrides_noop(self):
        m = _masters(items=[_item("W")])
        self.assertIs(apply_record_overrides(m, {}), m)
        self.assertIs(apply_record_overrides(m, None), m)

    def test_override_resolves_a_validation_error(self):
        # Missing state is an error; overriding the state should clear it.
        m = _masters(customers=[_party("X", LedgerState="")])
        before = {i.code for i in validate_masters(m).issues}
        self.assertIn("GST_STATE_MISSING", before)
        apply_record_overrides(m, {"Customer": {"X": {"LedgerState": "Gujarat"}}})
        after = {i.code for i in validate_masters(m).issues}
        self.assertNotIn("GST_STATE_MISSING", after)


class TestEditableReport(unittest.TestCase):
    def test_groups_carry_editable_fields_and_current_values(self):
        m = _masters(items=[_item("River Sand", hsn="")])
        payload = group_report(validate_masters(m), records_by_key(m))
        hsn_group = next(g for g in payload["groups"] if g["code"] == "HSN_MISSING")
        self.assertEqual(hsn_group["editable_fields"][0]["field"], "HSNCode")
        self.assertEqual(hsn_group["items"][0]["current"], {"HSNCode": ""})

    def test_no_lookup_means_no_current_values(self):
        m = _masters(items=[_item("River Sand", hsn="")])
        payload = group_report(validate_masters(m))  # no lookup
        hsn_group = next(g for g in payload["groups"] if g["code"] == "HSN_MISSING")
        self.assertNotIn("current", hsn_group["items"][0])
        # editable_fields metadata is still present.
        self.assertTrue(hsn_group["editable_fields"])

    def test_states_list_is_canonical_and_sorted(self):
        states = erpnext_states()
        self.assertIn("Gujarat", states)
        self.assertEqual(states, sorted(states))

    def test_duplicate_party_is_not_editable(self):
        self.assertNotIn("DUPLICATE_PARTY", EDITABLE_FIELDS)


if __name__ == "__main__":
    unittest.main()
