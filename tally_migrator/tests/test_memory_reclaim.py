"""The post-extraction memory reclaim (_malloc_trim) must be pure best-effort.

Dropping the parsed source frees the Python objects, but on glibc the freed pages stay
in the allocator, so RSS holds at the extraction high-water mark through the long import
(the measured cause of Frappe Cloud OOM pressure). ``_malloc_trim`` asks the C allocator
to return them. It must never raise or change a run's outcome - only its footprint - so
it degrades to a no-op wherever the symbol is absent (musl, macOS dev) or libc cannot be
resolved. It resolves libc via ``CDLL(None)`` first so it still works in a minimal
container where ``find_library('c')`` returns None.
"""
import types
import unittest
from unittest import mock

from tally_migrator.migration import master_migrator as mm


def _lib_with_trim(calls):
    """A stand-in libc whose ``malloc_trim`` records its argument (like a real _FuncPtr,
    it is a plain callable, not a bound method, so the code path matches production)."""
    def malloc_trim(n):
        calls.append(n)
        return 1
    return types.SimpleNamespace(malloc_trim=malloc_trim)


class TestMallocTrim(unittest.TestCase):
    def test_returns_bool_and_never_raises(self):
        # Real call on the test machine - must simply not raise and return a bool.
        self.assertIsInstance(mm._malloc_trim(), bool)

    def test_calls_malloc_trim_zero_when_available(self):
        calls = []
        # First candidate is CDLL(None); return a libc that has malloc_trim.
        with mock.patch("ctypes.CDLL", side_effect=lambda name: _lib_with_trim(calls)):
            self.assertTrue(mm._malloc_trim())
        self.assertEqual(calls, [0])   # malloc_trim(0) trims all arenas

    def test_works_when_find_library_returns_none(self):
        # The minimal-container case: find_library('c') is None, but CDLL(None) still
        # exposes malloc_trim. Must NOT silently no-op.
        calls = []
        with mock.patch("ctypes.util.find_library", return_value=None), \
             mock.patch("ctypes.CDLL", side_effect=lambda name: _lib_with_trim(calls)):
            self.assertTrue(mm._malloc_trim())
        self.assertEqual(calls, [0])

    def test_symbol_absent_everywhere_is_a_clean_noop(self):
        # No candidate libc exposes malloc_trim (musl / macOS).
        with mock.patch("ctypes.util.find_library", return_value=None), \
             mock.patch("ctypes.CDLL", side_effect=lambda name: types.SimpleNamespace()):
            self.assertFalse(mm._malloc_trim())

    def test_cdll_raises_for_every_candidate_is_a_clean_noop(self):
        with mock.patch("ctypes.util.find_library", return_value=None), \
             mock.patch("ctypes.CDLL", side_effect=OSError("cannot load")):
            self.assertFalse(mm._malloc_trim())

    def test_find_library_failure_is_swallowed(self):
        # find_library itself raising must not propagate; CDLL(None) is still tried.
        with mock.patch("ctypes.util.find_library", side_effect=OSError("boom")), \
             mock.patch("ctypes.CDLL", side_effect=OSError("cannot load")):
            self.assertFalse(mm._malloc_trim())

    def test_falls_through_to_a_later_candidate(self):
        # CDLL(None) fails to load, but a named soname works - the loop must keep going.
        calls = []
        def cdll(name):
            if name is None:
                raise OSError("no global handle")
            return _lib_with_trim(calls)
        with mock.patch("ctypes.util.find_library", return_value=None), \
             mock.patch("ctypes.CDLL", side_effect=cdll):
            self.assertTrue(mm._malloc_trim())
        self.assertEqual(calls, [0])


if __name__ == "__main__":
    unittest.main()
