"""
Regression test for ``api._raw_file_bytes`` strict-decode behaviour.

When ``File.get_content()`` hands back an already-decoded ``str`` (recent Frappe
decodes text uploads), the resolver re-reads the original bytes from disk so a
UTF-16 Tally export survives. If that disk re-read also fails, the only remaining
option is to re-encode the decoded str as latin-1 - which silently corrupts a
genuine UTF-16 export and imports wrong masters with no error.

By default we fail loud instead (``tally_migrator_strict_decode`` defaults to
True). Operators who knowingly handle latin-1 exports can opt back into the legacy
best-effort path. These tests pin both branches.
"""
import types
import unittest
from unittest import mock

from tally_migrator import api


def _str_content_doc():
    """A File doc whose get_content() returns a str and whose disk read fails."""
    return types.SimpleNamespace(
        get_content=lambda: "already-decoded text",
        get_full_path=lambda: "/nonexistent/path/does-not-exist.xml",
    )


class TestRawFileBytesStrictDecode(unittest.TestCase):
    def test_strict_default_throws_instead_of_corrupting(self):
        doc = _str_content_doc()
        # no flag set -> strict default (True)
        with mock.patch.object(api.frappe, "conf", {}), \
             mock.patch.object(api.frappe, "log_error"), \
             mock.patch.object(api.frappe, "get_traceback", return_value=""):
            with self.assertRaises(api.frappe.ValidationError):
                api._raw_file_bytes(doc)

    def test_opt_out_falls_back_to_latin1(self):
        doc = _str_content_doc()
        # strict disabled -> legacy best-effort re-encode path
        with mock.patch.object(
            api.frappe, "conf", {"tally_migrator_strict_decode": False}
        ), \
             mock.patch.object(api.frappe, "log_error"), \
             mock.patch.object(api.frappe, "get_traceback", return_value=""), \
             mock.patch.object(api, "_assert_within_size_limit"):
            out = api._raw_file_bytes(doc)
        self.assertEqual(out, b"already-decoded text")

    def test_failure_is_logged(self):
        doc = _str_content_doc()
        with mock.patch.object(api.frappe, "conf", {}), \
             mock.patch.object(api.frappe, "log_error") as log_error, \
             mock.patch.object(api.frappe, "get_traceback", return_value=""):
            with self.assertRaises(api.frappe.ValidationError):
                api._raw_file_bytes(doc)
        log_error.assert_called_once()


if __name__ == "__main__":
    unittest.main()
