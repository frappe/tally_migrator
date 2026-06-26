"""Phase 2 guard: the run endpoint must choose background-vs-synchronous from the
File's metadata alone (size / remote / zip), never by parsing. No site needed."""
import unittest
from types import SimpleNamespace
from unittest import mock

from tally_migrator import api


def _file(size=0, name="Master.xml", url="/files/Master.xml", remote=False):
    return SimpleNamespace(file_size=size, file_name=name, file_url=url, is_remote_file=remote)


class TestShouldRunAsync(unittest.TestCase):
    def _conf(self, mb=15):
        return mock.patch.object(api.frappe, "conf", {"tally_migrator_async_threshold_mb": mb})

    def test_small_plain_file_runs_sync(self):
        with self._conf(15):
            self.assertFalse(api._should_run_async(_file(size=2 * 1024 * 1024)))

    def test_large_plain_file_runs_async(self):
        with self._conf(15):
            self.assertTrue(api._should_run_async(_file(size=40 * 1024 * 1024)))

    def test_zip_always_async_even_when_small(self):
        # A small .zip still defers: compressed size understates the real work.
        with self._conf(15):
            self.assertTrue(api._should_run_async(
                _file(size=1 * 1024 * 1024, name="Master.zip", url="/files/Master.zip")))

    def test_remote_drive_link_always_async(self):
        # Size is unknown until the worker downloads it.
        with self._conf(15):
            self.assertTrue(api._should_run_async(_file(size=0, remote=True)))

    def test_threshold_is_config_overridable(self):
        f = _file(size=20 * 1024 * 1024)
        with self._conf(10):
            self.assertTrue(api._should_run_async(f))   # 20MB > 10MB
        with self._conf(50):
            self.assertFalse(api._should_run_async(f))   # 20MB < 50MB

    def test_zip_detected_from_url_when_name_blank(self):
        with self._conf(15):
            self.assertTrue(api._should_run_async(
                _file(size=1, name="", url="/files/export.zip")))


if __name__ == "__main__":
    unittest.main()
