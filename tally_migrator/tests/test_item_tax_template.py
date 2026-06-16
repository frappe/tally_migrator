"""
Phase 3 tests: item GST rate -> India Compliance GST Item Tax Template.

The combined GST rate is read per duty head (IGST == CGST + SGST), so a Cess
rate can never be mistaken for the GST rate. Each item is then linked to the
company's seeded template (matched by gst_treatment + gst_rate, not by name);
a taxable rate with no template warns rather than guessing.
"""
import unittest
from unittest import mock

from tally_migrator.tally.file_source import FileTallySource
from tally_migrator.erpnext.importers.items import ItemImporter
from tally_migrator.erpnext.importers import ImportResult


def _item_xml(name, rate_rows):
    rd = "".join(
        f"<RATEDETAILS.LIST><GSTRATEDUTYHEAD>{h}</GSTRATEDUTYHEAD>"
        + (f"<GSTRATE>{r}</GSTRATE>" if r is not None else "")
        + "</RATEDETAILS.LIST>"
        for h, r in rate_rows)
    return (f'<TALLYMESSAGE><STOCKITEM NAME="{name}"><NAME>{name}</NAME>'
            f'<GSTDETAILS.LIST><STATEWISEDETAILS.LIST><STATENAME>Any</STATENAME>'
            f'{rd}</STATEWISEDETAILS.LIST></GSTDETAILS.LIST></STOCKITEM></TALLYMESSAGE>')


class TestGstRateExtraction(unittest.TestCase):
    def _rates(self, *items):
        xml = "<ENVELOPE><BODY><IMPORTDATA><REQUESTDATA>" + "".join(items) + \
              "</REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>"
        return FileTallySource(xml).item_gst_rates()

    def test_picks_igst_not_cess(self):
        rates = self._rates(_item_xml("Widget", [
            ("CGST", " 9"), ("SGST/UTGST", " 9"), ("IGST", " 18"),
            ("Cess", " 1"),            # must NOT be mistaken for the GST rate
        ]))
        self.assertEqual(rates["Widget"], "18")

    def test_falls_back_to_cgst_plus_sgst_when_no_igst(self):
        rates = self._rates(_item_xml("Gadget", [("CGST", "6"), ("SGST/UTGST", "6")]))
        self.assertEqual(rates["Gadget"], "12")

    def test_no_rate_for_exempt_item(self):
        rates = self._rates(_item_xml("Rice", [("IGST", None)]))   # head present, no rate
        self.assertEqual(rates["Rice"], "")


class TestTreatmentLabel(unittest.TestCase):
    def test_labels(self):
        L = ItemImporter._gst_treatment_label
        self.assertEqual(L({"GSTTaxability": "Taxable"}), "Taxable")
        self.assertEqual(L({"GSTTaxability": "Nil Rated"}), "Nil-Rated")
        self.assertEqual(L({"GSTTaxability": "Exempt"}), "Exempted")
        self.assertEqual(L({"GSTTaxability": "Non-GST"}), "Non-GST")
        self.assertEqual(L({"GstApplicable": "Not Applicable"}), "Non-GST")
        self.assertEqual(L({"GSTTaxability": ""}), "Taxable")
        self.assertIsNone(L({"GSTTaxability": "Weird Value"}))


class TestTaxTemplateResolution(unittest.TestCase):
    def _imp(self):
        imp = ItemImporter(company="Frappe Tech", abbr="FT")
        imp._india_compliance = True
        return imp

    def test_resolves_links_and_warns_on_missing(self):
        imp = self._imp()
        res = ImportResult("Item")
        records = [
            {"_name": "A", "GSTTaxability": "Taxable", "GstRate": "18"},   # -> template
            {"_name": "B", "GSTTaxability": "Taxable", "GstRate": "3"},    # -> no template, warn
            {"_name": "C", "GSTTaxability": "Exempt",  "GstRate": ""},     # -> Exempted template
        ]

        def fake_get_all(doctype, filters=None, **kw):
            if filters.get("gst_treatment") == "Taxable" and filters.get("gst_rate") == 18:
                return ["GST 18% - FT"]
            if filters.get("gst_treatment") == "Exempted":
                return ["Exempted - FT"]
            return []
        with mock.patch("frappe.get_all", side_effect=fake_get_all):
            imp._resolve_tax_templates(records, res)

        self.assertEqual(imp._tax_templates.get("A"), "GST 18% - FT")
        self.assertEqual(imp._tax_templates.get("C"), "Exempted - FT")
        self.assertNotIn("B", imp._tax_templates)
        self.assertTrue(any(w["name"] == "B" and "3%" in w["reason"] for w in res.warnings))

    def test_no_india_compliance_is_noop(self):
        imp = ItemImporter(company="Frappe Tech", abbr="FT")
        imp._india_compliance = False
        res = ImportResult("Item")
        imp._resolve_tax_templates([{"_name": "A", "GSTTaxability": "Taxable", "GstRate": "18"}], res)
        self.assertEqual(imp._tax_templates, {})
        self.assertEqual(res.warnings, [])

    def test_build_doc_adds_resolved_template(self):
        imp = self._imp()
        imp._tax_templates = {"Pen": "GST 18% - FT"}
        doc = imp.build_doc({"_name": "Pen", "BaseUnits": "Nos", "GSTTaxability": "Taxable"})
        self.assertEqual(doc["taxes"], [{"item_tax_template": "GST 18% - FT"}])

    def test_build_doc_no_template_no_taxes_key(self):
        imp = self._imp()
        imp._tax_templates = {}
        doc = imp.build_doc({"_name": "Pen", "BaseUnits": "Nos", "GSTTaxability": "Taxable"})
        self.assertNotIn("taxes", doc)


if __name__ == "__main__":
    unittest.main()
