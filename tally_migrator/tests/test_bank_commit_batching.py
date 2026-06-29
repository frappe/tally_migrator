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
            fr.db.exists.return_value = False   # bank account name is free (no collision)
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


class TestUniqueBankAccountName(unittest.TestCase):
    """Tally repeats one holder name across several bank ledgers of a company; since
    ERPNext names a Bank Account 'account_name - bank', they would collide and all but
    the first drop. _unique_bank_account_name disambiguates so each keeps its details."""

    def test_returns_base_when_name_free(self):
        with mock.patch.object(banks, "frappe") as fr:
            fr.db.exists.return_value = False
            self.assertEqual(
                banks._unique_bank_account_name("ICONCEPT", "HDFC Bank", "2250"),
                "ICONCEPT")

    def test_appends_account_no_when_holder_taken(self):
        # base name collides; the variant with the account number is free.
        with mock.patch.object(banks, "frappe") as fr:
            fr.db.exists.side_effect = lambda dt, name: name == "ICONCEPT - HDFC Bank"
            self.assertEqual(
                banks._unique_bank_account_name("ICONCEPT", "HDFC Bank", "2250"),
                "ICONCEPT (2250)")

    def test_numeric_suffix_when_no_account_no(self):
        # base collides and there is no account number to disambiguate with.
        taken = {"ICONCEPT - HDFC Bank", "ICONCEPT 2 - HDFC Bank"}
        with mock.patch.object(banks, "frappe") as fr:
            fr.db.exists.side_effect = lambda dt, name: name in taken
            self.assertEqual(
                banks._unique_bank_account_name("ICONCEPT", "HDFC Bank", ""),
                "ICONCEPT 3")

    def test_blank_holder_returned_unchanged(self):
        with mock.patch.object(banks, "frappe") as fr:
            fr.db.exists.return_value = False
            self.assertEqual(banks._unique_bank_account_name("", "HDFC Bank", ""), "")


if __name__ == "__main__":
    unittest.main()
