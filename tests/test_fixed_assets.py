import unittest
from decimal import Decimal

from fixed_assets import _asset_period_amount


class FixedAssetDepreciationCalculationTests(unittest.TestCase):
    def setUp(self):
        self.asset = {
            "id": 1,
            "asset_code": "M-001",
            "asset_name": "Machine",
            "ready_date": "2026-01-16",
            "cost": "120000",
            "residual_value": "12000",
            "useful_life_months": 60,
            "depreciation_method": "straight_line",
            "annual_rate": "0",
        }

    def test_straight_line_full_month(self):
        amount = _asset_period_amount(self.asset, Decimal("0"), "2026-02")
        self.assertEqual(amount, Decimal("1800.00"))

    def test_straight_line_first_month_is_prorated_from_ready_date(self):
        amount = _asset_period_amount(self.asset, Decimal("0"), "2026-01")
        self.assertEqual(amount, Decimal("929.03"))

    def test_disposal_month_is_prorated_through_disposal_date(self):
        amount = _asset_period_amount(
            self.asset, Decimal("0"), "2026-01", cutoff_date="2026-01-20"
        )
        self.assertEqual(amount, Decimal("290.32"))

    def test_final_partial_month_clears_only_remaining_depreciable_amount(self):
        accumulated = Decimal("107129.03")
        amount = _asset_period_amount(self.asset, accumulated, "2031-01")
        self.assertEqual(amount, Decimal("870.97"))
        self.assertEqual(accumulated + amount, Decimal("108000.00"))

    def test_depreciation_never_exceeds_remaining_depreciable_base(self):
        amount = _asset_period_amount(self.asset, Decimal("107999.99"), "2026-02")
        self.assertEqual(amount, Decimal("0.01"))

    def test_declining_balance_uses_annual_rate_and_monthly_period(self):
        declining = dict(self.asset, depreciation_method="declining_balance", annual_rate="20")
        amount = _asset_period_amount(declining, Decimal("0"), "2026-02")
        self.assertEqual(amount, Decimal("2000.00"))

    def test_period_before_ready_date_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "قبل جاهزية"):
            _asset_period_amount(self.asset, Decimal("0"), "2025-12")

    def test_period_after_useful_life_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "انتهى العمر"):
            _asset_period_amount(self.asset, Decimal("0"), "2031-02")


if __name__ == "__main__":
    unittest.main()
