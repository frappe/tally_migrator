"""
Regression test for shared-file_url resolution in ``api._source_from_file``.

Byte-identical uploads are stored at one path in Frappe, so a single ``file_url``
can map to several File rows owned by different users. The resolver must pick the
row owned by the current user; otherwise a manager re-uploading an export someone
else already uploaded resolves to the other user's File and is wrongly refused
access ("You are not permitted to access this file").
"""
import types
import unittest
from unittest import mock

from tally_migrator import api


class TestSharedFileUrlResolution(unittest.TestCase):
    def _run(self, exists_rows, session_user="manager@example.com"):
        """Drive _source_from_file with a fake File table and capture the row picked."""
        # exists_rows: dict keyed by frozenset(filter items) -> File name (or None)
        calls = []

        def fake_exists(doctype, filters):
            calls.append(filters)
            return exists_rows.get(frozenset(filters.items()))

        picked = {}

        def fake_get_doc(doctype, name):
            picked["name"] = name
            return types.SimpleNamespace(
                name=name, owner="ignored", modified="t", file_name="x.xml"
            )

        prev_user = api.frappe.session.user
        api.frappe.session.user = session_user
        try:
            with mock.patch.object(api.frappe.db, "exists", side_effect=fake_exists), \
                 mock.patch.object(api.frappe, "get_doc", side_effect=fake_get_doc), \
                 mock.patch.object(api, "_assert_file_access"), \
                 mock.patch.object(api, "_raw_file_bytes", return_value=b"<x/>"), \
                 mock.patch.object(api, "_decode", return_value="<x/>"), \
                 mock.patch.object(api, "FileTallySource", side_effect=lambda s: ("src", s)), \
                 mock.patch.dict(api._SOURCE_CACHE, clear=True):
                file_doc, _ = api._source_from_file("/private/files/shared.xml")
        finally:
            api.frappe.session.user = prev_user
        return picked["name"], calls

    def test_prefers_row_owned_by_current_user(self):
        url = "/private/files/shared.xml"
        rows = {
            frozenset({"file_url": url, "owner": "manager@example.com"}.items()): "MINE",
            frozenset({"file_url": url}.items()): "OTHERS",  # the bare fallback
        }
        name, calls = self._run(rows)
        self.assertEqual(name, "MINE")
        # owner-scoped lookup must be the first query
        self.assertEqual(calls[0].get("owner"), "manager@example.com")

    def test_falls_back_to_any_row_when_user_owns_none(self):
        url = "/private/files/shared.xml"
        rows = {
            frozenset({"file_url": url, "owner": "manager@example.com"}.items()): None,
            frozenset({"file_url": url}.items()): "OTHERS",
        }
        name, _ = self._run(rows)
        # access is still enforced by _assert_file_access; resolution just falls back
        self.assertEqual(name, "OTHERS")

    def test_missing_file_raises(self):
        url = "/private/files/shared.xml"
        rows = {}  # nothing matches either query
        with self.assertRaises(api.frappe.DoesNotExistError):
            self._run(rows)


if __name__ == "__main__":
    unittest.main()
