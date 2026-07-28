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


class TestCompanyBankAccountSetsCompany(unittest.TestCase):
    """A company Bank Account must carry ``company`` explicitly: ERPNext requires it on a
    company account and never cross-checks it against the GL account, so leaning on
    frappe.new_doc's ambient default is a bug (blank on a no-default site -> hard failure;
    the wrong company on a multi-company site -> silent mis-attribution). A party account
    must NOT get a company (a party's bank account is not company-bound). The pre-existing
    mocked tests could not see this - a MagicMock swallows every attribute - so this fake
    records exactly which fields the helper assigns."""

    class _RecordingDoc:
        def __init__(self):
            object.__setattr__(self, "assigned", {})
            object.__setattr__(self, "name", "BA-0001")

        def __setattr__(self, key, value):
            if key not in ("assigned", "name"):
                self.assigned[key] = value
            object.__setattr__(self, key, value)

        def insert(self, **kwargs):
            pass

    def _assigned_fields(self, *, is_company, company="_TMTest Target Co"):
        doc = self._RecordingDoc()
        res = ImportResult("Account")
        with mock.patch.object(banks, "atomic", _noop_atomic), \
                mock.patch.object(banks, "frappe") as fr:
            fr.new_doc.return_value = doc
            fr.db.exists.return_value = False   # bank account name is free
            banks._insert_bank_account(
                account_name="Acme", bank="HDFC Bank", account_no="123", ifsc="",
                result=res, warn_name="Acme",
                gl_account="Bank - AC" if is_company else "",
                company=company if is_company else "",
                party_type="" if is_company else "Customer",
                party="" if is_company else "Acme",
                is_company=is_company, count_created=is_company)
        return doc.assigned

    def test_company_path_sets_company_and_flag(self):
        assigned = self._assigned_fields(is_company=True, company="_TMTest Target Co")
        self.assertEqual(assigned.get("company"), "_TMTest Target Co")
        self.assertEqual(assigned.get("is_company_account"), 1)
        self.assertEqual(assigned.get("account"), "Bank - AC")

    def test_party_path_does_not_set_company(self):
        assigned = self._assigned_fields(is_company=False)
        self.assertNotIn("company", assigned)
        self.assertEqual(assigned.get("party_type"), "Customer")


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
