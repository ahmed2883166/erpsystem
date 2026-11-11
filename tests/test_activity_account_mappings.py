import unittest

from chart_templates import (
    ACTIVITY_ACCOUNT_ROLES,
    ACTIVITY_POSTING_ACCOUNTS,
    CHARTS,
)


class ActivityAccountMappingTests(unittest.TestCase):
    def test_every_role_code_exists_as_a_detail_account_of_the_expected_type(self):
        for activity, roles in ACTIVITY_ACCOUNT_ROLES.items():
            chart = {row[0]: row for row in CHARTS[activity]}
            for role, codes in roles.items():
                expected_type = "liability" if role in {"payables", "current_liabilities"} else (
                    "expense" if role == "direct_cost" else "asset"
                )
                for code in codes:
                    with self.subTest(activity=activity, role=role, code=code):
                        self.assertIn(code, chart)
                        self.assertEqual(chart[code][2], expected_type)
                        self.assertEqual(chart[code][4], 0)

    def test_posting_accounts_match_each_activity_chart(self):
        for activity, mapping in ACTIVITY_POSTING_ACCOUNTS.items():
            chart = {row[0]: row for row in CHARTS[activity]}
            for key in ("ar", "revenue", "vat", "inventory", "cogs",
                        "purchase_inventory", "supplier_ap", "input_vat"):
                code = mapping[key]
                if code is None:
                    continue
                expected_type = {
                    "ar": "asset", "revenue": "revenue", "vat": "liability",
                    "inventory": "asset", "cogs": "expense",
                    "purchase_inventory": "asset", "supplier_ap": "liability",
                    "input_vat": "asset",
                }[key]
                with self.subTest(activity=activity, key=key, code=code):
                    self.assertIn(code, chart)
                    self.assertEqual(chart[code][2], expected_type)
                    self.assertEqual(chart[code][4], 0)

    def test_industrial_v1_uses_same_stock_control_for_purchases_and_sales(self):
        mapping = ACTIVITY_POSTING_ACCOUNTS["industrial"]
        self.assertEqual(mapping["purchase_inventory"], "1130")
        self.assertEqual(mapping["inventory"], "1130")
        self.assertEqual(mapping["vat"], "2120")

    def test_vat_control_code_matches_each_activity_chart(self):
        expected = {
            "commercial": "2130",
            "service": "2130",
            "medical": "2120",
            "restaurant": "2120",
            "industrial": "2120",
        }
        self.assertEqual(
            {activity: mapping["vat"] for activity, mapping in ACTIVITY_POSTING_ACCOUNTS.items()},
            expected,
        )


if __name__ == "__main__":
    unittest.main()
