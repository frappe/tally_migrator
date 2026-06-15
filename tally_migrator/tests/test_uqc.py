"""
Phase 1 (UQC) tests.

India Compliance stores the GST Unit Quantity Code globally in
GST Settings.gst_uom_map (uom -> gst_uom), not per-UOM. UnitImporter maps each
imported Tally UOM's REPORTINGUQCNAME ("NOS-NUMBERS") to the official GST option
("NOS (Numbers)"), appending only missing rows. Codes with no official match
(e.g. Tally's "REM-REAMS") are left unmapped with a warning, never guessed.
"""
import types
import unittest
from unittest import mock

from tally_migrator.erpnext.importers.units import UnitImporter
from tally_migrator.erpnext.importers import ImportResult

OPTIONS = "NOS (Numbers)\nKGS (Kilograms)\nBOX (Box)\nDOZ (Dozens)\nPCS (Pieces)\nOTH (Others)"


def _code_map():
    return {o.split("(")[0].strip().upper(): o for o in OPTIONS.splitlines()}


class TestUqcMapping(unittest.TestCase):
    def test_matches_by_code_case_insensitive(self):
        cm = _code_map()
        self.assertEqual(UnitImporter._uqc_to_gst_uom("NOS-NUMBERS", cm), "NOS (Numbers)")
        self.assertEqual(UnitImporter._uqc_to_gst_uom("KGS-KILOGRAMS", cm), "KGS (Kilograms)")
        self.assertEqual(UnitImporter._uqc_to_gst_uom("box-boxes", cm), "BOX (Box)")

    def test_blank_not_applicable_and_unmatched_return_none(self):
        cm = _code_map()
        self.assertIsNone(UnitImporter._uqc_to_gst_uom("REM-REAMS", cm))      # no official code
        self.assertIsNone(UnitImporter._uqc_to_gst_uom("", cm))
        self.assertIsNone(UnitImporter._uqc_to_gst_uom("\x04 Not Applicable", cm))
        self.assertIsNone(UnitImporter._uqc_to_gst_uom("Not Applicable", cm))

    def _fake_settings(self, existing=()):
        s = types.SimpleNamespace(
            gst_uom_map=[types.SimpleNamespace(uom=u, gst_uom=g) for u, g in existing],
            saved=False)
        s.append = lambda table, row: s.gst_uom_map.append(
            types.SimpleNamespace(uom=row["uom"], gst_uom=row["gst_uom"]))

        def _save(**kw):
            s.saved = True
        s.save = _save
        return s

    def test_map_uqcs_appends_missing_skips_existing_warns_unmatched(self):
        imp = UnitImporter("_T Co", "TC")
        res = ImportResult("UOM")
        units = [
            {"_name": "Nos",  "ReportingUQC": "NOS-NUMBERS"},
            {"_name": "Kg",   "ReportingUQC": "KGS-KILOGRAMS"},
            {"_name": "Ream", "ReportingUQC": "REM-REAMS"},          # no match -> warn
            {"_name": "Box",  "ReportingUQC": "\x04 Not Applicable"},  # skipped silently
            {"_name": "Pcs",  "ReportingUQC": "PCS-PIECES"},          # already mapped -> skip
        ]
        settings = self._fake_settings(existing=[("Pcs", "PCS (Pieces)")])
        meta = types.SimpleNamespace(
            get_field=lambda f: types.SimpleNamespace(options=OPTIONS))
        with mock.patch("frappe.db.exists", return_value=True), \
                mock.patch("frappe.get_meta", return_value=meta), \
                mock.patch("frappe.get_doc", return_value=settings), \
                mock.patch("frappe.db.commit"):
            imp._map_uqcs(units, res)

        mapped = {(r.uom, r.gst_uom) for r in settings.gst_uom_map}
        self.assertIn(("Nos", "NOS (Numbers)"), mapped)
        self.assertIn(("Kg", "KGS (Kilograms)"), mapped)
        self.assertFalse(any(r.uom == "Box" for r in settings.gst_uom_map))      # skipped
        self.assertEqual(sum(1 for r in settings.gst_uom_map if r.uom == "Pcs"), 1)  # not duped
        self.assertTrue(any(w["name"] == "Ream" for w in res.warnings))          # warned
        self.assertTrue(settings.saved)

    def test_no_india_compliance_is_noop(self):
        imp = UnitImporter("_T Co", "TC")
        res = ImportResult("UOM")
        with mock.patch("frappe.db.exists", return_value=False):
            imp._map_uqcs([{"_name": "Nos", "ReportingUQC": "NOS-NUMBERS"}], res)
        self.assertEqual(res.warnings, [])


class TestUqcExtraction(unittest.TestCase):
    def test_extraction_reads_latest_reporting_uqc(self):
        from tally_migrator.tally.file_source import FileTallySource
        from tally_migrator.tally.extractors import UNIT_FIELDS, UNIT_TAGS
        # Dated list with two entries; the most recent (last) wins.
        xml = (
            '<ENVELOPE><BODY><IMPORTDATA><REQUESTDATA>'
            '<TALLYMESSAGE><UNIT NAME="Nos"><NAME>Nos</NAME>'
            '<REPORTINGUQCDETAILS.LIST><APPLICABLEFROM>20200401</APPLICABLEFROM>'
            '<REPORTINGUQCNAME>OTH-OTHERS</REPORTINGUQCNAME></REPORTINGUQCDETAILS.LIST>'
            '<REPORTINGUQCDETAILS.LIST><APPLICABLEFROM>20260401</APPLICABLEFROM>'
            '<REPORTINGUQCNAME>NOS-NUMBERS</REPORTINGUQCNAME></REPORTINGUQCDETAILS.LIST>'
            '</UNIT></TALLYMESSAGE>'
            '</REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>'
        )
        rows = FileTallySource(xml).get_collection("Unit", UNIT_FIELDS, UNIT_TAGS)
        self.assertEqual(rows[0]["ReportingUQC"], "NOS-NUMBERS")


if __name__ == "__main__":
    unittest.main()
