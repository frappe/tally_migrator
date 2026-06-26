"""Unit tests for the zipped-XML and Google-Drive-link import paths.

No Frappe site required: ``frappe.throw`` raises, which is all these assertions
need. The Drive download is exercised against a mocked ``requests`` so no network
is touched.
"""
import io
import unittest
import zipfile
from unittest import mock

from tally_migrator.tally.file_source import unzip_if_zip
from tally_migrator import api


XML_BYTES = b"<ENVELOPE><BODY>hello tally</BODY></ENVELOPE>"
CAP = 10 * 1024 * 1024  # 10 MB cap for the tests


def _zip(members: dict, compression=zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


class TestUnzipIfZip(unittest.TestCase):
    def test_plain_xml_passes_through_unchanged(self):
        # No ZIP magic - returned byte-for-byte, no parsing attempted.
        self.assertIs(unzip_if_zip(XML_BYTES, CAP), XML_BYTES)

    def test_str_passes_through(self):
        text = XML_BYTES.decode()
        self.assertIs(unzip_if_zip(text, CAP), text)

    def test_single_xml_member_is_extracted(self):
        raw = _zip({"Master.xml": XML_BYTES})
        self.assertEqual(unzip_if_zip(raw, CAP), XML_BYTES)

    def test_macos_resource_forks_are_ignored(self):
        # A Finder-created zip carries __MACOSX/._Master.xml alongside the real file.
        raw = _zip({"Master.xml": XML_BYTES, "__MACOSX/._Master.xml": b"junk"})
        self.assertEqual(unzip_if_zip(raw, CAP), XML_BYTES)

    def test_no_xml_member_rejected(self):
        raw = _zip({"notes.txt": b"nothing here"})
        with self.assertRaises(Exception):
            unzip_if_zip(raw, CAP)

    def test_multiple_xml_members_rejected(self):
        raw = _zip({"a.xml": XML_BYTES, "b.xml": XML_BYTES})
        with self.assertRaises(Exception):
            unzip_if_zip(raw, CAP)

    def test_corrupt_zip_rejected(self):
        # ZIP magic but garbage after it.
        with self.assertRaises(Exception):
            unzip_if_zip(b"PK\x03\x04corrupt-not-a-real-archive", CAP)

    def test_unreadable_member_rejected_gracefully(self):
        # The central directory opens fine but the member's stored bytes are
        # clobbered, so archive.open(...).read() fails (bad CRC). Must surface as a
        # clean frappe.throw, not an unhandled 500.
        raw = _zip({"Master.xml": b"C" * 4096}, compression=zipfile.ZIP_STORED)
        corrupt = bytearray(raw)
        corrupt[40:80] = b"\x00" * 40   # trash the stored member payload, leave the dir intact
        with self.assertRaises(Exception):
            unzip_if_zip(bytes(corrupt), CAP)

    def test_zip_bomb_declared_size_rejected(self):
        # Highly compressible 50 MB member, but the cap is 10 MB.
        raw = _zip({"Master.xml": b"A" * (50 * 1024 * 1024)})
        with self.assertRaises(Exception):
            unzip_if_zip(raw, CAP)

    def test_member_at_cap_is_allowed(self):
        payload = b"B" * CAP
        raw = _zip({"Master.xml": payload})
        self.assertEqual(unzip_if_zip(raw, CAP), payload)


class TestParseDriveId(unittest.TestCase):
    def test_file_d_link(self):
        url = "https://drive.google.com/file/d/1A2b3C4d5E6f7G8h9I0j/view?usp=sharing"
        self.assertEqual(api._parse_drive_id(url), "1A2b3C4d5E6f7G8h9I0j")

    def test_open_id_link(self):
        url = "https://drive.google.com/open?id=1A2b3C4d5E6f7G8h9I0j"
        self.assertEqual(api._parse_drive_id(url), "1A2b3C4d5E6f7G8h9I0j")

    def test_uc_download_link(self):
        url = "https://drive.google.com/uc?export=download&id=1A2b3C4d5E6f7G8h9I0j"
        self.assertEqual(api._parse_drive_id(url), "1A2b3C4d5E6f7G8h9I0j")

    def test_docs_d_link(self):
        url = "https://docs.google.com/spreadsheets/d/1A2b3C4d5E6f7G8h9I0j/edit"
        self.assertEqual(api._parse_drive_id(url), "1A2b3C4d5E6f7G8h9I0j")

    def test_bare_id_accepted(self):
        self.assertEqual(api._parse_drive_id("1A2b3C4d5E6f7G8h9I0j"), "1A2b3C4d5E6f7G8h9I0j")

    def test_non_google_host_rejected(self):
        with self.assertRaises(Exception):
            api._parse_drive_id("https://evil.example.com/file/d/1A2b3C4d5E6f7G8h9I0j/view")

    def test_internal_host_rejected_no_ssrf(self):
        with self.assertRaises(Exception):
            api._parse_drive_id("http://169.254.169.254/latest/meta-data/")

    def test_empty_rejected(self):
        with self.assertRaises(Exception):
            api._parse_drive_id("   ")

    def test_google_link_without_id_rejected(self):
        with self.assertRaises(Exception):
            api._parse_drive_id("https://drive.google.com/drive/folders/")


class _FakeResp:
    def __init__(self, chunks, status=200):
        self._chunks = chunks
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}")

    def iter_content(self, chunk_size=0):
        return iter(self._chunks)

    def close(self):
        pass


class TestDownloadDriveFile(unittest.TestCase):
    def _patch_conf(self):
        # _max_upload_bytes reads frappe.conf; keep it small and deterministic.
        return mock.patch.object(api.frappe, "conf", {"tally_migrator_max_upload_mb": 10})

    def test_happy_path_returns_bytes(self):
        with self._patch_conf(), \
                mock.patch("requests.get", return_value=_FakeResp([XML_BYTES])):
            self.assertEqual(api._download_drive_file("abc1234567"), XML_BYTES)

    def test_html_interstitial_rejected(self):
        page = b"<!DOCTYPE html><html><body>Sign in</body></html>"
        with self._patch_conf(), \
                mock.patch("requests.get", return_value=_FakeResp([page])):
            with self.assertRaises(Exception):
                api._download_drive_file("abc1234567")

    def test_oversized_stream_rejected(self):
        big = [b"X" * (4 * 1024 * 1024)] * 4  # 16 MB streamed, cap is 10 MB
        with self._patch_conf(), \
                mock.patch("requests.get", return_value=_FakeResp(big)):
            with self.assertRaises(Exception):
                api._download_drive_file("abc1234567")

    def test_empty_response_rejected(self):
        with self._patch_conf(), \
                mock.patch("requests.get", return_value=_FakeResp([])):
            with self.assertRaises(Exception):
                api._download_drive_file("abc1234567")


if __name__ == "__main__":
    unittest.main()
