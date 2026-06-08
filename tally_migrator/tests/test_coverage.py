"""Tests for the field-coverage report. Pure, runs locally:
    python -m unittest tally_migrator.tests.test_coverage
"""
import unittest
from unittest import mock

from tally_migrator.migration import coverage as cov
from tally_migrator.migration.coverage import coverage_report, read_tags


class _Src:
    """Stub source exposing only raw_tags(obj_type)."""
    def __init__(self, tags_by_type):
        self._t = tags_by_type

    def raw_tags(self, obj_type):
        return self._t.get(obj_type, {})


def _tag(count=1, sample="", records=None):
    return {"count": count, "sample": sample, "records": records or []}


class TestCoverage(unittest.TestCase):
    def test_clean_when_only_mapped_fields(self):
        # Every real Ledger tag the extractor reads → nothing unmapped.
        tags = {t: _tag() for t in read_tags("Ledger")}
        report = coverage_report(_Src({"Ledger": tags}))
        self.assertTrue(report["clean"])
        self.assertEqual(report["unmapped_field_count"], 0)
        self.assertEqual(report["types"], [])

    def test_flags_udf_field(self):
        tags = {
            "NAME": _tag(),
            "GSTREGISTRATIONNUMBER": _tag(),          # mapped
            "CUSTOMERCATEGORY": _tag(3, "Wholesale", ["Acme", "Bolt Co"]),  # UDF
        }
        report = coverage_report(_Src({"Ledger": tags}))
        self.assertFalse(report["clean"])
        self.assertEqual(report["unmapped_field_count"], 1)
        led = report["types"][0]
        self.assertEqual(led["entity_type"], "Ledger")
        u = led["unmapped"][0]
        self.assertEqual(u["field"], "CUSTOMERCATEGORY")
        self.assertEqual(u["count"], 3)
        self.assertEqual(u["sample"], "Wholesale")
        self.assertEqual(u["examples"], ["Acme", "Bolt Co"])

    def test_ignores_housekeeping_tags(self):
        tags = {"NAME": _tag(), "GUID": _tag(), "MASTERID": _tag(), "ALTERID": _tag()}
        report = coverage_report(_Src({"Ledger": tags}))
        self.assertTrue(report["clean"])

    def test_multiple_entity_types(self):
        report = coverage_report(_Src({
            "Ledger": {"CUSTOMERCATEGORY": _tag()},  # a UDF, genuinely unmapped
            "Stock Item": {"BATCHNAME": _tag(), "PARENT": _tag()},  # PARENT is mapped
        }))
        self.assertEqual(report["unmapped_field_count"], 2)
        kinds = {t["entity_type"] for t in report["types"]}
        self.assertEqual(kinds, {"Ledger", "Stock Item"})

    def test_flags_read_but_not_written_field(self):
        # A field the extractor fetches (mapped) but no importer persists (not in
        # WRITTEN_FIELDS) must be reported as 'unwritten', not silently "clean".
        patched = {**cov.WRITTEN_FIELDS, "Ledger": ["Name", "Parent"]}
        with mock.patch.object(cov, "WRITTEN_FIELDS", patched):
            report = coverage_report(_Src({
                "Ledger": {"PARENT": _tag(), "OPENINGBALANCE": _tag(2, "5000 Dr", ["Cash"])},
            }))
        self.assertFalse(report["clean"])
        self.assertEqual(report["unmapped_field_count"], 0)
        self.assertEqual(report["unwritten_field_count"], 1)
        led = report["types"][0]
        self.assertEqual(led["unmapped"], [])
        self.assertEqual(led["unwritten"][0]["field"], "OPENINGBALANCE")
        self.assertEqual(led["unwritten"][0]["sample"], "5000 Dr")

    def test_real_tally_tags_are_not_flagged(self):
        # A genuine export uses LEDSTATENAME / EMAIL / ADDRESS.LIST instead of the
        # flat canonical tags — the parser reads them, so coverage must treat them
        # as covered, not "not migrated".
        tags = {
            "NAME": _tag(),
            "LEDSTATENAME": _tag(),
            "EMAIL": _tag(),
            "ADDRESS.LIST": _tag(),
        }
        report = coverage_report(_Src({"Ledger": tags}))
        self.assertTrue(report["clean"], report["types"])
        # And item price/cost revision-list containers on Stock Item.
        item_tags = {"NAME": _tag(), "STANDARDPRICELIST.LIST": _tag(),
                     "STANDARDCOSTLIST.LIST": _tag()}
        rep2 = coverage_report(_Src({"Stock Item": item_tags}))
        self.assertTrue(rep2["clean"], rep2["types"])

    def test_real_mapping_has_no_unwritten_gap(self):
        # Guard: with the real WRITTEN_FIELDS, every fetched item field is written
        # (regression guard for the GST_Applicable/GSTTypeName silent-drop bug).
        tags = {t: _tag() for t in read_tags("Stock Item")}
        report = coverage_report(_Src({"Stock Item": tags}))
        self.assertEqual(report["unwritten_field_count"], 0)
        self.assertTrue(report["clean"])


if __name__ == "__main__":
    unittest.main()
