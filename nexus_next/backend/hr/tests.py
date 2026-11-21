from decimal import Decimal

from django.test import SimpleTestCase

from hr.models import PayrollResult, PayrollComponent
from hr.services import calculate_component, money


class PayrollCalculationTests(SimpleTestCase):
    def test_monthly_daily_divisor_rounds_money(self):
        self.assertEqual(money(Decimal("12000") * Decimal("15") / Decimal("30")), Decimal("6000.00"))

    def test_fixed_and_percent_components(self):
        fixed = PayrollComponent(method=PayrollComponent.Method.FIXED, default_value=Decimal("750"))
        percent = PayrollComponent(method=PayrollComponent.Method.PERCENT_BASIC, default_value=Decimal("10"))
        self.assertEqual(calculate_component(fixed, Decimal("750"), Decimal("12000"), Decimal("30"), Decimal("0"), Decimal("30"), Decimal("0")), Decimal("750.00"))
        self.assertEqual(calculate_component(percent, Decimal("10"), Decimal("12000"), Decimal("30"), Decimal("0")), Decimal("1200.00"))

    def test_friday_double_day_uses_two_daily_rates(self):
        friday = PayrollComponent(method="friday_double_day", default_value=Decimal("0"))
        self.assertEqual(calculate_component(friday, None, Decimal("12000"), Decimal("30"), Decimal("0"), Decimal("30"), Decimal("2")), Decimal("1600.00"))

    def test_employer_share_is_displayed_on_both_sides_without_changing_net(self):
        result = PayrollResult(
            earnings_total=Decimal("15000"),
            employee_deductions_total=Decimal("1500"),
            employer_share_total=Decimal("1875"),
            employer_share_display_addition=Decimal("1875"),
            employer_share_display_offset=Decimal("1875"),
            net_pay=Decimal("13500"),
        )
        self.assertEqual(result.displayed_earnings_total, Decimal("16875"))
        self.assertEqual(result.displayed_deductions_total, Decimal("3375"))
        self.assertEqual(result.net_pay, Decimal("13500"))
        result.clean()

    def test_employer_share_must_be_a_paired_display_only_amount(self):
        result = PayrollResult(
            earnings_total=Decimal("15000"),
            employee_deductions_total=Decimal("1500"),
            employer_share_total=Decimal("1875"),
            employer_share_display_addition=Decimal("1875"),
            employer_share_display_offset=Decimal("0"),
            net_pay=Decimal("13500"),
        )
        with self.assertRaises(Exception):
            result.clean()
