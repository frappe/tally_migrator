"""The bank helpers must commit ONLY on the non-batched company path.

On the batched party path a mid-batch ``frappe.db.commit()`` would flush the
whole in-flight batch and defeat the commit-batching speed-up, so the helpers
leave their docs in the batch for ``BaseImporter.run``'s batch commit. On the
standalone company path (``AccountImporter``) a later account's full rollback
would wipe a still-uncommitted Bank / Bank Account, so there the commit is kept.

Pure behaviour test: every frappe touchpoint and ``atomic`` is mocked, so it
runs without a bound site.
"""
import contextlib
import unittest
from unittest import mock

from tally_migrator.erpnext.importers import banks
from tally_migrator.erpnext.importers.base import ImportResult


@contextlib.contextmanager
def _noop_atomic():
    yield


class TestBankHelpersCommitOnlyForCompany(unittest.TestCase):
    def test_ensure_bank_commits_on_company_path(self):
        with mock.patch.object(banks, "atomic", _noop_atomic), \
                mock.patch.object(banks, "frappe") as fr:
            fr.db.exists.return_value = False
            out = banks._ensure_bank("HDFC Bank", ImportResult("Account"), is_company=True)
        self.assertEqual(out, "HDFC Bank")
        fr.db.commit.assert_called_once()

    def test_ensure_bank_does_not_commit_on_party_path(self):
        res = ImportResult("Customer")
        with mock.patch.object(banks, "atomic", _noop_atomic), \
                mock.patch.object(banks, "frappe") as fr:
            fr.db.exists.return_value = False
            out = banks._ensure_bank("HDFC Bank", res)        # is_company defaults False
        self.assertEqual(out, "HDFC Bank")
        fr.db.commit.assert_not_called()
        # Revert tracking must still happen without the commit.
        self.assertIn({"name": "HDFC Bank", "doctype": "Bank"}, res.created_docs)

    def _call_insert(self, *, is_company):
        fake = mock.MagicMock(); fake.name = "BA-0001"
        res = ImportResult("Account")
        with mock.patch.object(banks, "atomic", _noop_atomic), \
                mock.patch.object(banks, "frappe") as fr:
            fr.new_doc.return_value = fake
            out = banks._insert_bank_account(
                account_name="Acme", bank="HDFC Bank", account_no="123",
                ifsc="HDFC0000001", result=res, warn_name="Acme",
                gl_account="Bank - AC" if is_company else "",
                party_type="" if is_company else "Customer",
                party="" if is_company else "Acme",
                is_company=is_company, count_created=is_company)
        return out, fr.db.commit

    def test_insert_bank_account_commits_on_company_path(self):
        out, commit = self._call_insert(is_company=True)
        self.assertEqual(out, "BA-0001")
        commit.assert_called_once()

    def test_insert_bank_account_does_not_commit_on_party_path(self):
        out, commit = self._call_insert(is_company=False)
        self.assertEqual(out, "BA-0001")
        commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
