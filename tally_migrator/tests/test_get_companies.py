"""
Regression test for api.get_companies.

The wizard's target-company picker must work for the Tally Migration Manager
role, which deliberately holds no Company read permission. get_companies is a
role-gated endpoint that reads with ignore_permissions so the picker is not
empty without widening the role.
"""
import unittest
from unittest import mock

from tally_migrator import api


class TestGetCompanies(unittest.TestCase):
    def test_gated_and_ignores_permissions(self):
        with mock.patch.object(api.frappe, "only_for") as only_for, \
             mock.patch.object(
                 api.frappe, "get_all", return_value=[{"name": "Acme"}]
             ) as get_all:
            result = api.get_companies()

        # access is gated to the migrator roles
        only_for.assert_called_once_with(api.ALLOWED_ROLES)
        # the read bypasses the (absent) Company read permission
        _, kwargs = get_all.call_args
        self.assertEqual(get_all.call_args.args[0], "Company")
        self.assertTrue(kwargs.get("ignore_permissions"))
        self.assertEqual(result, [{"name": "Acme"}])


if __name__ == "__main__":
    unittest.main()
