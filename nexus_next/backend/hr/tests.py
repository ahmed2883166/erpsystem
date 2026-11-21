from decimal import Decimal

from django.test import SimpleTestCase, TestCase
from django.contrib.auth.models import Permission
from rest_framework.test import APIClient

from accounts.models import User
from companies.models import Branch, Company, CompanyMembership, Currency

from hr.models import PayrollResult, PayrollComponent
from hr.services import calculate_component, money


class PayrollCalculationTests(SimpleTestCase):
    def test_monthly_daily_divisor_rounds_money(self):
        self.assertEqual(money(Decimal("12000") * Decimal("15") / Decimal("30")), Decimal("6000.00"))

    def test_fixed_and_percent_components(self):
        fixed = PayrollComponent(method=PayrollComponent.Method.FIXED, default_value=Decimal("750"))
        percent = PayrollComponent(method=PayrollComponent.Method.PERCENT_BASIC, default_value=Decimal("10"))
        self.assertEqual(calculate_component(fixed, Decimal("750"), Decimal("12000"), Decimal("30"), Decimal("0"), Decimal("30"), Decimal("0")), Decimal("750.00"))
        self.assertEqual(calculate_component(percent, Decimal("10"), Decimal("12000"), Decimal("30"), Decimal("0"), Decimal("30"), Decimal("0")), Decimal("1200.00"))

    def test_friday_double_day_uses_two_daily_rates(self):
        friday = PayrollComponent(method="friday_double_day", default_value=Decimal("0"))
        self.assertEqual(calculate_component(friday, None, Decimal("12000"), Decimal("30"), Decimal("0"), Decimal("30"), Decimal("2")), Decimal("1600.00"))

    def test_employee_insurance_uses_configured_capped_base(self):
        component = PayrollComponent(method=PayrollComponent.Method.EMPLOYEE_INSURANCE)
        config = {"insurance": {"min_base": 2000, "max_base": 6000, "employee_rate": 10, "employer_rate": 15}}
        amount = calculate_component(component, None, Decimal("8000"), Decimal("30"), Decimal("0"), Decimal("30"), Decimal("0"), config, Decimal("8000"), Decimal("8000"))
        self.assertEqual(amount, Decimal("600.00"))

    def test_progressive_tax_uses_configured_annual_brackets(self):
        component = PayrollComponent(method=PayrollComponent.Method.PROGRESSIVE_TAX)
        config = {"tax": {"annual_personal_exemption": 0, "annual_brackets": [
            {"up_to": 60000, "rate": 10}, {"up_to": 120000, "rate": 20}, {"up_to": None, "rate": 30}
        ]}}
        amount = calculate_component(component, None, Decimal("10000"), Decimal("30"), Decimal("0"), Decimal("30"), Decimal("0"), config, Decimal("10000"), Decimal("10000"))
        self.assertEqual(amount, Decimal("1500.00"))

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


class EmployeePayrollReportApiTests(TestCase):
    def setUp(self):
        self.currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.company = Company.objects.create(
            code="HR-REPORT", name_ar="شركة تقرير المرتبات", country="EG",
            activity="commercial", base_currency=self.currency,
        )
        self.branch = Branch.objects.create(
            company=self.company, code="HQ", name_ar="المركز الرئيسي", is_head_office=True,
        )
        self.employee = Employee.objects.create(
            company=self.company, code="EMP-001", full_name_ar="موظف اختبار",
            branch=self.branch, hire_date=date(2026, 1, 1),
            basic_salary=Decimal("10000"), currency=self.currency,
        )
        self.cycle = PayrollCycle.objects.create(
            company=self.company, period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31), payment_date=date(2026, 2, 1),
            status=PayrollCycle.Status.PAID,
        )
        self.result = PayrollResult.objects.create(
            cycle=self.cycle, employee=self.employee,
            basic_salary_snapshot=Decimal("10000"),
            earnings_total=Decimal("12000"),
            employee_deductions_total=Decimal("1500"),
            employer_share_total=Decimal("1800"),
            employer_share_display_addition=Decimal("1800"),
            employer_share_display_offset=Decimal("1800"),
            net_pay=Decimal("10500"), company_cost=Decimal("13800"),
        )
        PayrollResultLine.objects.create(
            result=self.result, code_snapshot="BASIC", name_snapshot="الراتب الأساسي",
            kind_snapshot="earning", amount=Decimal("10000"),
        )
        PayrollResultLine.objects.create(
            result=self.result, code_snapshot="BONUS", name_snapshot="حافز",
            kind_snapshot="earning", amount=Decimal("2000"),
        )
        PayrollResultLine.objects.create(
            result=self.result, code_snapshot="EMPLOYER_SHARE_DISPLAY",
            name_snapshot="حصة صاحب العمل للعرض", kind_snapshot="earning",
            amount=Decimal("1800"), display_only=True,
        )
        self.user = User.objects.create_user(username="hr_report_user", password="test-password")
        self.user.user_permissions.add(Permission.objects.get(codename="view_payrollcycle"))
        CompanyMembership.objects.create(
            user=self.user, company=self.company, role="accountant", all_branches=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_employee_pay_report_returns_posted_payroll_totals_and_dynamic_components(self):
        response = self.client.get(
            f"/api/hr/payroll-cycles/employee-pay-report/?company={self.company.pk}"
            "&date_from=2026-01-01&date_to=2026-01-31"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["employee_count"], 1)
        employee = response.data["employees"][0]
        self.assertEqual(employee["employee_code"], "EMP-001")
        self.assertEqual(employee["earnings"], Decimal("12000"))
        self.assertEqual(employee["employee_deductions"], Decimal("1500"))
        self.assertEqual(employee["employer_share"], Decimal("1800"))
        self.assertEqual(employee["net_pay"], Decimal("10500"))
        self.assertEqual(employee["company_cost"], Decimal("13800"))
        self.assertEqual(
            {line["code"] for line in employee["components"]},
            {"BASIC", "BONUS"},
        )
        self.assertEqual(len(employee["periods"]), 1)

    def test_employee_pay_report_respects_period_and_rejects_invalid_dates(self):
        outside = self.client.get(
            f"/api/hr/payroll-cycles/employee-pay-report/?company={self.company.pk}"
            "&date_from=2026-02-01&date_to=2026-02-28"
        )
        invalid = self.client.get(
            f"/api/hr/payroll-cycles/employee-pay-report/?company={self.company.pk}"
            "&date_from=2026-02-28&date_to=2026-02-01"
        )
        self.assertEqual(outside.status_code, 200)
        self.assertEqual(outside.data["employee_count"], 0)
        self.assertEqual(invalid.status_code, 400)

    def test_employee_pay_report_does_not_expose_other_company(self):
        other_currency = Currency.objects.create(code="SAR", name_ar="ريال سعودي")
        other = Company.objects.create(
            code="HR-OTHER", name_ar="شركة أخرى", country="SA",
            activity="service", base_currency=other_currency,
        )
        response = self.client.get(
            f"/api/hr/payroll-cycles/employee-pay-report/?company={other.pk}"
            "&date_from=2026-01-01&date_to=2026-01-31"
        )
        self.assertEqual(response.status_code, 403)
