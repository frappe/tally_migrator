"""Phase 3 party-import performance + country-alias fixes.

Two changes, both data-neutral:

  * ``normalize_country`` - Tally writes a country as free text; ERPNext's
    Address.country is a Link to Country, so an unrecognised abbreviation ("UAE")
    loses the whole address. A minimal, evidence-based alias map rewrites only
    unambiguous abbreviations to their canonical ERPNext Country name and passes
    everything else through byte-for-byte.
  * ``_address_primary_sync_suspended`` - ERPNext's ``ERPNextAddress.on_update``
    runs, on every Address save, a ``SELECT ... FROM tabCustomer WHERE
    customer_primary_address = <this address>`` scan (2,511 calls / 3.4s on a real
    book) to refresh a customer's cached primary-address display. In the migrator
    that scan is always empty (we link the primary AFTER insert with a direct
    ``set_value``) and we write the display ourselves, so it is pure overhead. The
    suspension skips ONLY that sync and still forwards to any base ``on_update``.

Pure/Frappe-tier: ``normalize_country`` needs no site; the suspension tests use a
synthetic class hierarchy (the MRO drop-in) and a real class-attribute swap/restore,
so no DB is touched.
"""
import contextlib
import unittest

from tally_migrator.tally.mappings import normalize_country
from tally_migrator.erpnext.importers import party


class TestNormalizeCountry(unittest.TestCase):
    """Only unambiguous aliases are rewritten; everything else passes through."""

    def test_known_aliases_map_to_canonical_erpnext_names(self):
        cases = {
            "UAE": "United Arab Emirates",
            "uae": "United Arab Emirates",
            "U.A.E.": "United Arab Emirates",
            "USA": "United States",
            "usa": "United States",
            "U.S.A.": "United States",
            "US": "United States",
            "U.S.": "United States",
            "UK": "United Kingdom",
            "uk": "United Kingdom",
            "U.K.": "United Kingdom",
        }
        for raw, expected in cases.items():
            self.assertEqual(normalize_country(raw), expected, raw)

    def test_real_country_names_pass_through_verbatim(self):
        # A value that is already a valid ERPNext Country name is never altered.
        for name in ["India", "United States", "United Arab Emirates",
                     "Germany", "Sri Lanka", "United Kingdom"]:
            self.assertEqual(normalize_country(name), name)

    def test_unknown_values_pass_through_trimmed(self):
        # Not an alias -> returned as-is (trimmed); never guessed at.
        self.assertEqual(normalize_country("  Narnia  "), "Narnia")
        self.assertEqual(normalize_country("Bharat"), "Bharat")

    def test_blank_stays_blank(self):
        # Blank -> "" so the caller falls back to the company country.
        for blank in ["", "   ", None]:
            self.assertEqual(normalize_country(blank), "")

    def test_case_and_dot_insensitive_but_not_over_eager(self):
        # "us" is an alias; a longer word that merely starts with it is not.
        self.assertEqual(normalize_country("us"), "United States")
        self.assertEqual(normalize_country("USB"), "USB")          # not "United States"
        self.assertEqual(normalize_country("United"), "United")    # not mapped


class TestAddressOnUpdateDropIn(unittest.TestCase):
    """The MRO drop-in skips ERPNextAddress's own sync but forwards a base on_update."""

    def _run(self, mid_cls, runtime_cls):
        inst = runtime_cls()
        inst.calls = []
        fn = party._make_address_on_update_skip_primary(mid_cls)
        fn(inst)
        return inst.calls

    def test_forwards_to_a_base_on_update_and_skips_the_sync(self):
        class Base:
            def on_update(self):
                self.calls.append("base")

        class Mid(Base):                       # stand-in for ERPNextAddress
            def on_update(self):
                self.calls.append("redundant-sync")

        class Runtime(Mid):                    # the composed controller subclass
            pass

        # Base's on_update runs (forward-safe); the redundant sync does NOT.
        self.assertEqual(self._run(Mid, Runtime), ["base"])

    def test_no_base_on_update_means_a_clean_no_op(self):
        class Mid:                             # only ERPNextAddress defines on_update
            def on_update(self):
                self.calls.append("redundant-sync")

        class Runtime(Mid):
            pass

        # Nothing above Mid defines on_update -> the sync is simply skipped.
        self.assertEqual(self._run(Mid, Runtime), [])

    def test_missing_class_in_mro_is_handled(self):
        # Defensive: a self whose MRO does not contain the given class -> no crash,
        # no base call (there is nothing after an absent anchor).
        class Unrelated:
            pass

        class Mid:
            def on_update(self):
                self.calls.append("sync")

        inst = Unrelated()
        inst.calls = []
        party._make_address_on_update_skip_primary(Mid)(inst)
        self.assertEqual(inst.calls, [])


class TestAddressPrimarySyncSuspended(unittest.TestCase):
    """The context manager swaps ERPNextAddress.on_update in and restores it out."""

    def setUp(self):
        try:
            from erpnext.accounts.custom.address import ERPNextAddress  # noqa: F401
        except Exception:
            self.skipTest("erpnext not installed")
        self.ERPNextAddress = ERPNextAddress

    def test_swaps_inside_and_restores_after(self):
        original = self.ERPNextAddress.on_update
        with party._address_primary_sync_suspended():
            self.assertIsNot(self.ERPNextAddress.on_update, original)
        self.assertIs(self.ERPNextAddress.on_update, original)

    def test_restores_even_on_exception(self):
        original = self.ERPNextAddress.on_update
        with self.assertRaises(RuntimeError):
            with party._address_primary_sync_suspended():
                raise RuntimeError("boom")
        self.assertIs(self.ERPNextAddress.on_update, original)

    def test_patched_on_update_does_not_scan_customers(self):
        # Inside the CM, the installed on_update must not run the tabCustomer scan.
        from unittest import mock
        with party._address_primary_sync_suspended():
            patched = self.ERPNextAddress.on_update
            # Build a minimal instance whose MRO ends at ERPNextAddress with no base
            # on_update, so the drop-in is a clean no-op; assert get_all is untouched.
            class _Runtime(self.ERPNextAddress):
                pass
            inst = _Runtime.__new__(_Runtime)
            with mock.patch.object(party.frappe.db, "get_all") as g:
                patched(inst)
            g.assert_not_called()


if __name__ == "__main__":
    unittest.main()
