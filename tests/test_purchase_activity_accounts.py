import unittest
from unittest.mock import patch

from purchases import _activity_posting_accounts


class PurchaseActivityAccountMappingTests(unittest.TestCase):
    def test_ambiguous_grni_role_requires_explicit_posting_account(self):
        profile = {
            "posting_accounts": {"supplier_ap": "2110"},
            "account_roles": {"grni": ["2190", "2191"]},
        }
        with patch("company_activities.resolve_activity_profile", return_value=profile):
            with self.assertRaisesRegex(ValueError, "أكثر من حساب"):
                _activity_posting_accounts(object(), "commercial")

    def test_single_grni_role_is_used_as_legacy_fallback(self):
        profile = {
            "posting_accounts": {"supplier_ap": "2110"},
            "account_roles": {"grni": ["2190"]},
        }
        with patch("company_activities.resolve_activity_profile", return_value=profile):
            result = _activity_posting_accounts(object(), "commercial")
        self.assertEqual(result["grni"], "2190")

    def test_explicit_grni_posting_account_takes_precedence(self):
        profile = {
            "posting_accounts": {"supplier_ap": "2110", "grni": "2192"},
            "account_roles": {"grni": ["2190", "2191"]},
        }
        with patch("company_activities.resolve_activity_profile", return_value=profile):
            result = _activity_posting_accounts(object(), "commercial")
        self.assertEqual(result["grni"], "2192")


if __name__ == "__main__":
    unittest.main()
