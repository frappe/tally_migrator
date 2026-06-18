"""Unit tests for UOM map and state map - pure Python, no Frappe needed."""
import unittest

from tally_migrator.tally.mappings import (
    UOM_MAP, TALLY_STATE_MAP, DEFAULT_UOM, gst_category_from_type,
)


class TestUomMap(unittest.TestCase):
    def test_common_aliases_resolve(self):
        cases = {
            "Nos": "Nos", "PCS": "Nos", "Pcs": "Nos",
            "Kgs": "Kg", "KG": "Kg",
            "Ltr": "Litre", "LTR": "Litre",
            "Mtr": "Metre", "MTR": "Metre",
        }
        for tally_uom, expected in cases.items():
            with self.subTest(tally_uom=tally_uom):
                self.assertEqual(UOM_MAP[tally_uom], expected)

    def test_unknown_uom_not_in_map(self):
        self.assertNotIn("UNKNOWN_UNIT", UOM_MAP)

    def test_default_uom_is_nos(self):
        self.assertEqual(DEFAULT_UOM, "Nos")


class TestStateMap(unittest.TestCase):
    def test_all_states_present(self):
        expected_states = [
            "Maharashtra", "Karnataka", "Tamil Nadu", "Gujarat",
            "Delhi", "Uttar Pradesh", "West Bengal", "Rajasthan",
            "Telangana", "Andhra Pradesh",
        ]
        for state in expected_states:
            with self.subTest(state=state):
                self.assertIn(state, TALLY_STATE_MAP)

    def test_jk_variant_normalised(self):
        self.assertEqual(TALLY_STATE_MAP["Jammu & Kashmir"], "Jammu and Kashmir")
        self.assertEqual(TALLY_STATE_MAP["Jammu and Kashmir"], "Jammu and Kashmir")

    def test_total_state_count(self):
        # 28 states + 8 UTs = 36 entries (Tally uses some alternative spellings)
        self.assertGreaterEqual(len(TALLY_STATE_MAP), 36)


class TestGstCategoryFromType(unittest.TestCase):
    def test_known_types(self):
        self.assertEqual(gst_category_from_type("Regular"), "Registered Regular")
        self.assertEqual(gst_category_from_type("Composition"), "Registered Composition")
        self.assertEqual(gst_category_from_type("Consumer"), "Unregistered")
        self.assertEqual(gst_category_from_type("SEZ"), "SEZ")

    def test_compound_sez_type_maps_to_sez(self):
        # Tally exports SEZ as the compound registration type "Regular - SEZ"
        # (the bare "SEZ" the table holds never appears in a real export), so the
        # SEZ token must be recognised inside the string, not only as an exact key.
        self.assertEqual(gst_category_from_type("Regular - SEZ"), "SEZ")
        self.assertEqual(gst_category_from_type("regular - sez"), "SEZ")
        # The token match must not fire on substrings of unrelated words.
        self.assertEqual(gst_category_from_type("Deemed Export"), "Deemed Export")

    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(gst_category_from_type("  reGular "), "Registered Regular")

    def test_blank_or_unknown_returns_empty(self):
        self.assertEqual(gst_category_from_type(""), "")
        self.assertEqual(gst_category_from_type(None), "")
        self.assertEqual(gst_category_from_type("Something Else"), "")
