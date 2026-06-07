"""Tests for the offline data-quality validation engine.

Pure logic, no Frappe — runnable locally:
    python -m unittest tally_migrator.tests.test_validation
"""
import unittest

from tally_migrator.tally.extractors import ExtractedMasters
from tally_migrator.validation.engine import (
    validate_gstin, gstin_check_digit, infer_gst_category,
    pin_state_conflict, normalize_party_name, find_duplicate_groups,
    validate_masters, group_report,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _valid_gstin(first14="27AAPFU0939F1Z"):
    """Build a structurally valid GSTIN by appending the correct check digit."""
    return first14 + gstin_check_digit(first14)


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
    return ExtractedMasters(
        customers=customers or [], suppliers=suppliers or [],
        items=items or [], warehouses=[],
    )


# ── GSTIN checksum + structure ────────────────────────────────────────────────

class TestGstin(unittest.TestCase):
    def test_valid_gstin_passes(self):
        ok, reason = validate_gstin(_valid_gstin())
        self.assertTrue(ok, reason)

    def test_wrong_check_digit_fails(self):
        good = _valid_gstin()
        bad = good[:14] + ("A" if good[14] != "A" else "B")
        ok, reason = validate_gstin(bad)
        self.assertFalse(ok)
        self.assertIn("checksum", reason)

    def test_bad_length_fails(self):
        ok, reason = validate_gstin("27AAPFU0939F1Z")  # 14 chars
        self.assertFalse(ok)
        self.assertIn("length", reason)

    def test_malformed_pattern_fails(self):
        ok, _ = validate_gstin("AA27PFU0939F1ZX")  # letters/digits swapped
        self.assertFalse(ok)

    def test_unknown_state_code_fails(self):
        body = "99AAPFU0939F1Z"
        ok, reason = validate_gstin(body + gstin_check_digit(body))
        self.assertFalse(ok)
        self.assertIn("state code", reason)

    def test_empty_is_invalid(self):
        ok, reason = validate_gstin("")
        self.assertFalse(ok)
        self.assertEqual(reason, "empty")


# ── GST category inference ────────────────────────────────────────────────────

class TestGstCategory(unittest.TestCase):
    def test_valid_gstin_is_registered(self):
        self.assertEqual(infer_gst_category(_valid_gstin(), "India"), "Registered Regular")

    def test_blank_india_is_unregistered(self):
        self.assertEqual(infer_gst_category("", "India"), "Unregistered")

    def test_blank_foreign_is_overseas(self):
        self.assertEqual(infer_gst_category("", "United States"), "Overseas")

    def test_invalid_gstin_falls_back_to_unregistered(self):
        self.assertEqual(infer_gst_category("BADGSTIN", "India"), "Unregistered")


# ── PIN ↔ state ───────────────────────────────────────────────────────────────

class TestPinState(unittest.TestCase):
    def test_conflict_detected(self):
        self.assertEqual(pin_state_conflict("400001", "Delhi"), "Maharashtra")

    def test_match_no_conflict(self):
        self.assertIsNone(pin_state_conflict("400001", "Maharashtra"))

    def test_short_pin_ignored(self):
        self.assertIsNone(pin_state_conflict("400", "Delhi"))


# ── Party de-duplication ──────────────────────────────────────────────────────

class TestDedup(unittest.TestCase):
    def test_normalize_strips_suffix_and_punct(self):
        self.assertEqual(normalize_party_name("Reliance Industries Ltd."), "reliance")

    def test_fuzzy_name_variants_group(self):
        parties = [
            _party("Reliance Industries"),
            _party("Reliance Ind."),
            _party("RELIANCE industries ltd"),
            _party("Tata Steel"),
        ]
        groups = find_duplicate_groups(parties)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 3)

    def test_same_gstin_groups_even_if_names_differ(self):
        g = _valid_gstin()
        parties = [
            _party("Acme", GSTRegistrationNumber=g),
            _party("Acme Trading Co", GSTRegistrationNumber=g),
        ]
        self.assertEqual(len(find_duplicate_groups(parties)), 1)

    def test_distinct_parties_do_not_group(self):
        parties = [_party("Tata Steel"), _party("Infosys")]
        self.assertEqual(find_duplicate_groups(parties), [])


# ── Master-level rules ────────────────────────────────────────────────────────

class TestValidateMasters(unittest.TestCase):
    def _codes(self, report):
        return {i.code for i in report.issues}

    def test_invalid_gstin_is_error(self):
        m = _masters(customers=[_party("X", GSTRegistrationNumber="27BADGSTIN0000")])
        report = validate_masters(m)
        self.assertIn("GSTIN_INVALID", self._codes(report))
        self.assertTrue(report.has_errors)

    def test_missing_state_is_error(self):
        m = _masters(customers=[_party("X", LedgerState="")])
        self.assertIn("GST_STATE_MISSING", self._codes(validate_masters(m)))

    def test_missing_hsn_is_warning(self):
        m = _masters(items=[_item("Widget", hsn="")])
        report = validate_masters(m)
        self.assertIn("HSN_MISSING", self._codes(report))
        self.assertFalse(report.has_errors)  # warning only

    def test_item_code_collision_is_error(self):
        # "A/B" and "A-B" both normalize to item_code "A-B".
        m = _masters(items=[_item("A/B"), _item("A-B")])
        report = validate_masters(m)
        self.assertIn("ITEM_CODE_COLLISION", self._codes(report))

    def test_duplicate_party_is_warning(self):
        m = _masters(customers=[_party("Reliance Industries"), _party("Reliance Ind.")])
        self.assertIn("DUPLICATE_PARTY", self._codes(validate_masters(m)))

    def test_clean_masters_no_issues(self):
        m = _masters(
            customers=[_party("Tata Steel", GSTRegistrationNumber=_valid_gstin())],
            items=[_item("Widget")],
        )
        report = validate_masters(m)
        self.assertEqual(report.issues, [])

    def test_totals_counted(self):
        m = _masters(customers=[_party("A")], suppliers=[_party("B")], items=[_item("C")])
        report = validate_masters(m)
        self.assertEqual(report.totals["Customer"], 1)
        self.assertEqual(report.totals["Supplier"], 1)
        self.assertEqual(report.totals["Item"], 1)


# ── Report shaping ────────────────────────────────────────────────────────────

class TestReport(unittest.TestCase):
    def test_summary_and_as_dict(self):
        m = _masters(customers=[_party("X", LedgerState="")], items=[_item("W", hsn="")])
        report = validate_masters(m)
        self.assertTrue(report.summary_lines())
        d = report.as_dict()
        self.assertIn("issues", d)
        self.assertEqual(d["errors"], len(report.errors))
        self.assertEqual(d["warnings"], len(report.warnings))


class TestGroupReport(unittest.TestCase):
    def test_collapses_same_code_and_orders_errors_first(self):
        # 3 customers all missing GST state (error) + 1 item missing HSN (warning).
        m = _masters(
            customers=[_party(n, LedgerState="") for n in ("A", "B", "C")],
            items=[_item("W", hsn="")],
        )
        out = group_report(validate_masters(m))
        self.assertEqual(out["error_count"], 3)
        self.assertEqual(out["warning_count"], 1)
        self.assertFalse(out["clean"])
        # GST_STATE_MISSING collapses 3 issues into one group of 3 items.
        state_group = next(g for g in out["groups"] if g["code"] == "GST_STATE_MISSING")
        self.assertEqual(len(state_group["items"]), 3)
        # Errors come before warnings.
        self.assertEqual(out["groups"][0]["severity"], "error")

    def test_clean_report(self):
        out = group_report(validate_masters(_masters()))
        self.assertTrue(out["clean"])
        self.assertEqual(out["groups"], [])


if __name__ == "__main__":
    unittest.main()
