"""Unit tests for UOM map and state map — pure Python, no Frappe needed."""
import unittest

from tally_migrator.tally.mappings import UOM_MAP, TALLY_STATE_MAP, DEFAULT_UOM


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
