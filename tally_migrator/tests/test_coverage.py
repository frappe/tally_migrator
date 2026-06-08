"""Tests for the field-coverage report. Pure, runs locally:
    python -m unittest tally_migrator.tests.test_coverage
"""
import unittest

from tally_migrator.migration.coverage import coverage_report, MAPPED_FIELDS, _norm


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
        # Every Ledger tag is a mapped field → nothing unmapped.
        tags = {_norm(f): _tag() for f in MAPPED_FIELDS["Ledger"]}
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
            "Ledger": {"CREDITLIMIT": _tag()},
            "Stock Item": {"BATCHNAME": _tag(), "PARENT": _tag()},  # PARENT is mapped
        }))
        self.assertEqual(report["unmapped_field_count"], 2)
        kinds = {t["entity_type"] for t in report["types"]}
        self.assertEqual(kinds, {"Ledger", "Stock Item"})


if __name__ == "__main__":
    unittest.main()
