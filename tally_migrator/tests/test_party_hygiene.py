"""Party data-hygiene: phone salvage and GSTIN<->state authority.

Two related fixes, both following the importer's established "validate the dirty field,
drop just that field, keep the record" pattern (as it already does for email, GSTIN and
PIN):

  * ``_clean_phone`` - a phone ERPNext rejects used to fail the WHOLE Contact/Address,
    silently losing the party's email and name too. Now a bad phone is salvaged or
    dropped, keeping the rest. Verbatim-if-valid, so good data (including international
    numbers ERPNext accepts) is never altered, and junk is never turned into a number.
  * ``_address_gstin`` / ``_resolve_state`` - a structurally valid GSTIN whose state code
    contradicts Tally's ledger state used to fail the whole address on India Compliance's
    "First 2 digits of GSTIN should match ..." rule. The GSTIN is now authoritative (its
    code IS the GST state), derived through IC's own state<->number map so it round-trips
    through IC's validation.

Frappe-tier (imports ``tally_migrator.erpnext.importers.party`` -> ``frappe``). The DB is
mocked, so no site is touched; ``validate_phone_number`` / ``validate_gstin`` and (when
installed) India Compliance's ``STATE_NUMBERS`` are exercised for real.
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


class TestCleanPhone(unittest.TestCase):
    """`_clean_phone` against ERPNext's real validator (frappe.utils.validate_phone_number)."""

    def test_valid_numbers_pass_through_verbatim(self):
        # No-override guarantee: anything ERPNext accepts is returned byte-for-byte.
        for good in [
            "9876543210",
            "+91 98765 43210",
            "98765-43210",
            "(011) 2345-6789",
            "+1 415 555 2671",       # US - the validator is not India-specific
            "+44 20 7946 0958",      # UK
            "+971 4 123 4567",       # UAE
        ]:
            self.assertEqual(party._clean_phone(good), good, f"altered a valid number: {good!r}")

    def test_whitespace_wrapped_valid_number_is_untouched(self):
        # ERPNext accepts it (its validator strips only for the check, then stores raw),
        # so we must return it exactly - not a trimmed version.
        self.assertEqual(party._clean_phone("  9876543210  "), "  9876543210  ")

    def test_integer_input_is_stringified(self):
        self.assertEqual(party._clean_phone(9876543210), "9876543210")

    def test_empty_and_none(self):
        self.assertEqual(party._clean_phone(""), "")
        self.assertEqual(party._clean_phone(None), "")
        self.assertEqual(party._clean_phone("   "), "")

    def test_salvages_trailing_invisible_char(self):
        # Tally's trailing bidi mark (U+202C) - stripped, number kept.
        self.assertEqual(party._clean_phone("6005-593802‬"), "6005-593802")

    def test_salvages_leading_text_guard_apostrophe(self):
        # Excel/Tally text guard - peeled off the end, number kept (Tier 2).
        self.assertEqual(party._clean_phone("'628065239"), "628065239")
        self.assertEqual(party._clean_phone('"9876543210"'), "9876543210")

    def test_drops_names_and_placeholders_never_fabricates(self):
        # A name / placeholder is not a number - dropped, never mined for digits.
        # (Note: "-" or "()" ARE accepted by ERPNext's lenient validator, so _clean_phone
        # stores them verbatim - being stricter than ERPNext would be its own override.)
        for junk in ["NOBLE INFOMATIQUE", "Balvinder Pa", "XXXXXXXXXX", "n/a", "abc"]:
            self.assertEqual(party._clean_phone(junk), "", f"fabricated from junk: {junk!r}")

    def test_internal_junk_is_never_mined(self):
        # Letters BETWEEN digits can't be peeled (only the ends are), so an alphanumeric
        # value is dropped whole - "98abc76" never becomes "9876".
        self.assertEqual(party._clean_phone("98abc76"), "")
        self.assertEqual(party._clean_phone("011-2345 Ph 98765"), "")  # two labelled bits -> drop

    def test_leading_label_is_peeled_to_the_number(self):
        # A label prefix is wrapper cruft on the end, so it peels off and the contiguous
        # number is recovered (same class as the apostrophe case). The number is never
        # fabricated - it appears verbatim and contiguous in the input.
        self.assertEqual(party._clean_phone("Ph: 98765"), " 98765")

    def test_drops_overlong_number(self):
        # ERPNext caps at 20 chars; a 23-digit value fails there too, so we drop it
        # (never silently truncate).
        self.assertEqual(party._clean_phone("1" * 23), "")


class TestStateByGstNumber(unittest.TestCase):
    """`_state_by_gst_number` must be the exact inverse of IC's STATE_NUMBERS so any
    state we derive from a GSTIN round-trips through IC's own validation."""

    def setUp(self):
        try:
            from india_compliance.gst_india.constants import STATE_NUMBERS  # noqa: F401
        except Exception:
            self.skipTest("India Compliance not installed")
        self.STATE_NUMBERS = STATE_NUMBERS

    def test_known_codes_map_to_ic_canonical_names(self):
        inv = party._state_by_gst_number()
        self.assertEqual(inv.get("04"), "Chandigarh")
        self.assertEqual(inv.get("27"), "Maharashtra")
        self.assertEqual(inv.get("03"), "Punjab")

    def test_is_exact_inverse_roundtrip(self):
        # For every code we resolve, IC maps the resulting state back to that same code -
        # so IC's "state number must match gstin[:2]" check can never reject it.
        inv = party._state_by_gst_number()
        self.assertTrue(inv, "inverse map unexpectedly empty on an IC site")
        for code, state in inv.items():
            self.assertEqual(self.STATE_NUMBERS[state], code)

    def test_legacy_daman_code_25_absent(self):
        # Post-2020 merger: IC has no code 25 - so a legacy 25-GSTIN is not representable
        # and must be handled by dropping the GSTIN, not by inventing a state.
        self.assertEqual(party._state_by_gst_number().get("25", ""), "")


class TestAddressGstin(unittest.TestCase):
    """`_address_gstin` - keep a GSTIN only when IC will accept it against the state."""

    # A real, checksum-valid GSTIN for Chandigarh (state code 04).
    GSTIN_04 = "04AABCU9603R1ZV"

    def setUp(self):
        self.result = ImportResult("Address")
        self._self = types.SimpleNamespace()   # _address_gstin uses no instance state

    def _call(self, gstin, state, country="India"):
        return party.PartyImporter._address_gstin(
            self._self, gstin, state, country, "ACME", self.result)

    def test_structurally_invalid_gstin_dropped(self):
        self.assertEqual(self._call("NOTAGSTIN", "Chandigarh"), "")
        self.assertEqual(self.result.warnings, [])   # invalid GSTIN is silent here (pre-flight owns it)

    def test_valid_gstin_matching_state_kept(self):
        if not party._state_by_gst_number():
            self.skipTest("India Compliance not installed")
        self.assertEqual(self._call(self.GSTIN_04, "Chandigarh"), self.GSTIN_04)
        self.assertEqual(self.result.warnings, [])

    def test_valid_gstin_mismatched_state_dropped_with_warning(self):
        if not party._state_by_gst_number():
            self.skipTest("India Compliance not installed")
        # GSTIN says 04 (Chandigarh) but the address state is Punjab -> IC would reject the
        # whole address; we keep the address and drop the GSTIN, with a visible warning.
        self.assertEqual(self._call(self.GSTIN_04, "Punjab"), "")
        self.assertTrue(any("GSTIN not set" in w["reason"] for w in self.result.warnings))

    def test_non_india_address_keeps_gstin_untouched(self):
        # IC does not cross-check a foreign address, so prior behaviour is preserved.
        self.assertEqual(self._call(self.GSTIN_04, "", country="United States"), self.GSTIN_04)
        self.assertEqual(self.result.warnings, [])

    def test_ic_absent_keeps_valid_gstin(self):
        with mock.patch.object(party, "_state_by_gst_number", return_value={}):
            self.assertEqual(self._call(self.GSTIN_04, "Anything"), self.GSTIN_04)
        self.assertEqual(self.result.warnings, [])


class TestResolveStateGstinAuthority(unittest.TestCase):
    """`_resolve_state` prefers a valid GSTIN's (IC-canonical) state over the ledger."""

    def setUp(self):
        if not party._state_by_gst_number():
            self.skipTest("India Compliance not installed")
        self._self = types.SimpleNamespace(
            _state_from_pincode=lambda pin: "")

    def _resolve(self, data):
        return party.PartyImporter._resolve_state(self._self, data)

    def test_gstin_wins_over_conflicting_ledger_state(self):
        # Ledger says Punjab, GSTIN says 04 (Chandigarh) -> Chandigarh, so it matches the
        # GSTIN we will attach and IC accepts the address.
        state = self._resolve({"GSTRegistrationNumber": "04AABCU9603R1ZV",
                               "LedgerState": "Punjab"})
        self.assertEqual(state, "Chandigarh")

    def test_ledger_used_when_no_gstin(self):
        state = self._resolve({"LedgerState": "Maharashtra"})
        self.assertEqual(state, "Maharashtra")

    def test_invalid_gstin_falls_back_to_ledger(self):
        state = self._resolve({"GSTRegistrationNumber": "GARBAGE",
                               "LedgerState": "Maharashtra"})
        self.assertEqual(state, "Maharashtra")


class TestSaveContactRecoversRest(unittest.TestCase):
    """The key win: a junk phone must no longer cost the party its email and name."""

    def _run_save_contact(self, data):
        result = ImportResult("Contact")
        made = {}

        class _FakeContact:
            def __init__(self):
                self.name = "CONTACT-0001"
                self.first_name = ""
                self.email_ids = []
                self.phone_nos = []
                self.links = []
                made["doc"] = self

            def append(self, table, row):
                getattr(self, table).append(row)

            def insert(self, **kw):
                made["inserted"] = True

        with mock.patch.object(party.frappe, "new_doc", side_effect=lambda *a, **k: _FakeContact()), \
             mock.patch.object(party, "atomic", _noop_atomic):
            name = party.PartyImporter._save_contact(
                types.SimpleNamespace(), "ACME", "Supplier", data, result)
        return name, made, result

    def test_junk_phone_keeps_email_and_name(self):
        name, made, result = self._run_save_contact({
            "LedgerContact": "Priya",
            "LedgerPhone": "NOBLE INFOMATIQUE",     # junk
            "LedgerEmail": "priya@acme.example",
        })
        self.assertEqual(name, "CONTACT-0001")            # contact WAS created
        doc = made["doc"]
        self.assertEqual(doc.first_name, "Priya")         # name kept
        self.assertEqual([e["email_id"] for e in doc.email_ids], ["priya@acme.example"])
        self.assertEqual(doc.phone_nos, [])               # junk phone dropped, not stored
        self.assertTrue(any("phone number skipped" in w["reason"] for w in result.warnings))

    def test_valid_mobile_and_junk_phone_keeps_mobile(self):
        name, made, result = self._run_save_contact({
            "LedgerContact": "Sales",
            "LedgerMobile": "9876543210",
            "LedgerPhone": "call us",               # junk
        })
        doc = made["doc"]
        self.assertEqual([p["phone"] for p in doc.phone_nos], ["9876543210"])
        self.assertEqual(doc.phone_nos[0]["is_primary_mobile_no"], 1)
        self.assertTrue(any("phone number skipped" in w["reason"] for w in result.warnings))

    def test_no_contact_when_nothing_valid_remains(self):
        name, made, result = self._run_save_contact({
            "LedgerContact": "Ghost",
            "LedgerPhone": "XXXXXXXXXX",             # junk, and no email/mobile
        })
        self.assertEqual(name, "")                        # nothing to create
        self.assertNotIn("doc", made)


if __name__ == "__main__":
    unittest.main()
