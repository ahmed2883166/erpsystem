from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase, TestCase
from django.core.exceptions import ValidationError
from django.contrib.auth.models import Permission
from rest_framework.test import APIClient

from accounts.models import User
from companies.models import Branch, Company, CompanyMembership, Currency, FiscalYear
from accounting.models import AccountRole, ChartOfAccount, JournalEntry
from treasury.models import TreasuryAccount, TreasuryTransaction
from hr.services import post_payroll_cycle, complete_payroll_payment

from hr.models import Employee, EmployeeSalaryHistory, PayrollCycle, PayrollResult, PayrollResultLine, PayrollComponent, LeaveRequest, AttendanceRecord, EmployeeLoan, LoanInstallment, PayrollRuleSet, EmployeeComponent
from hr.services import calculate_component, money, salary_for_date


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

    def test_egypt_2026_tax_selects_the_high_income_statutory_column(self):
        component = PayrollComponent(method=PayrollComponent.Method.PROGRESSIVE_TAX)
        from hr.services import EGYPT_2026_TAX_BRACKET_GROUPS
        config = {"tax": {
            "annual_personal_exemption": "20000",
            "annual_bracket_groups": EGYPT_2026_TAX_BRACKET_GROUPS,
        }}
        amount = calculate_component(
            component, None, Decimal("2000000") / Decimal("12"), Decimal("30"),
            Decimal("0"), Decimal("30"), Decimal("0"), config,
            Decimal("2000000") / Decimal("12"), Decimal("2000000") / Decimal("12"),
        )
        self.assertEqual(amount, Decimal("42875.00"))

    def test_egypt_2026_tax_uses_the_zero_and_lower_brackets_below_600k(self):
        component = PayrollComponent(method=PayrollComponent.Method.PROGRESSIVE_TAX)
        from hr.services import EGYPT_2026_TAX_BRACKET_GROUPS
        config = {"tax": {
            "annual_personal_exemption": "20000",
            "annual_bracket_groups": EGYPT_2026_TAX_BRACKET_GROUPS,
        }}
        amount = calculate_component(
            component, None, Decimal("50000"), Decimal("30"), Decimal("0"),
            Decimal("30"), Decimal("0"), config, Decimal("50000"), Decimal("50000"),
        )
        self.assertEqual(amount, Decimal("9979.17"))

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
        employer_component = PayrollComponent.objects.create(
            company=self.company, code="ER-SHARE", name_ar="حصة صاحب العمل",
            kind=PayrollComponent.Kind.EMPLOYER_SHARE, method=PayrollComponent.Method.FIXED,
        )
        PayrollResultLine.objects.create(
            result=self.result, component=employer_component,
            code_snapshot="ER-SHARE", name_snapshot="حصة صاحب العمل",
            kind_snapshot="employer_share", amount=Decimal("1800"), display_only=True,
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

    def test_attendance_cannot_be_changed_or_deleted_after_payroll_is_paid(self):
        self.cycle.status = PayrollCycle.Status.CALCULATED
        self.cycle.save(update_fields=("status", "updated_at"))
        attendance = AttendanceRecord.objects.create(
            employee=self.employee, work_date=date(2026, 1, 15), status=AttendanceRecord.Status.PRESENT,
        )
        self.cycle.status = PayrollCycle.Status.PAID
        self.cycle.save(update_fields=("status", "updated_at"))
        attendance.status = AttendanceRecord.Status.ABSENT
        with self.assertRaises(ValidationError):
            attendance.save()
        with self.assertRaises(ValidationError):
            attendance.delete()
        with self.assertRaises(ValidationError):
            AttendanceRecord.objects.create(
                employee=self.employee, work_date=date(2026, 1, 16), status=AttendanceRecord.Status.PRESENT,
            )

    def test_employee_basic_salary_cannot_be_changed_outside_salary_history(self):
        self.employee.basic_salary = Decimal("12000")
        with self.assertRaises(ValidationError):
            self.employee.save(update_fields=("basic_salary", "updated_at"))

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
            {"BASIC", "BONUS", "ER-SHARE"},
        )
        self.assertEqual(len(employee["periods"]), 1)

    def test_employee_component_assignment_rejects_overlapping_effective_dates(self):
        component = PayrollComponent.objects.create(
            company=self.company, code="BONUS-OVERLAP", name_ar="بدل ثابت",
            kind=PayrollComponent.Kind.EARNING, method=PayrollComponent.Method.FIXED,
        )
        first = self.client.post("/api/hr/employee-components/", {
            "employee": self.employee.pk, "component": component.pk, "value": "500",
            "effective_from": "2026-01-01", "effective_to": "2026-12-31", "is_active": True,
        }, format="json")
        second = self.client.post("/api/hr/employee-components/", {
            "employee": self.employee.pk, "component": component.pk, "value": "700",
            "effective_from": "2026-06-01", "effective_to": "2027-01-31", "is_active": True,
        }, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 400, second.data)
        self.assertEqual(EmployeeComponent.objects.filter(employee=self.employee, component=component).count(), 1)

    def test_manual_loan_installments_cannot_exceed_loan_principal(self):
        loan = EmployeeLoan.objects.create(
            employee=self.employee, reference="LOAN-CAP", issue_date=date(2026, 1, 1),
            principal=Decimal("200"), installment_count=2, installment_amount=Decimal("100"),
            outstanding_balance=Decimal("200"),
        )
        first = self.client.post("/api/hr/loan-installments/", {
            "loan": loan.pk, "due_date": "2026-05-01", "amount": "100",
        }, format="json")
        over = self.client.post("/api/hr/loan-installments/", {
            "loan": loan.pk, "due_date": "2026-06-01", "amount": "101",
        }, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(over.status_code, 400, over.data)
        self.assertEqual(LoanInstallment.objects.filter(loan=loan).count(), 1)

    def test_manager_can_delete_employee_only_when_no_linked_hr_records_exist(self):
        manager = User.objects.create_user(username="hr_employee_delete_manager", password="test-password")
        CompanyMembership.objects.create(
            user=manager, company=self.company, role="admin", all_branches=True,
        )
        removable = Employee.objects.create(
            company=self.company, code="EMP-DELETE", full_name_ar="موظف للحذف",
            branch=self.branch, hire_date=date(2026, 1, 1),
            basic_salary=Decimal("5000"), currency=self.currency,
        )
        linked = Employee.objects.create(
            company=self.company, code="EMP-LINKED", full_name_ar="موظف مرتبط",
            branch=self.branch, hire_date=date(2026, 1, 1),
            basic_salary=Decimal("5000"), currency=self.currency,
        )
        AttendanceRecord.objects.create(
            employee=linked, work_date=date(2026, 4, 1), status=AttendanceRecord.Status.PRESENT,
        )
        self.client.force_authenticate(manager)
        deleted = self.client.delete(f"/api/hr/employees/{removable.pk}/")
        blocked = self.client.delete(f"/api/hr/employees/{linked.pk}/")
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(blocked.status_code, 400, blocked.data)
        self.assertTrue(Employee.objects.filter(pk=linked.pk).exists())

    def test_manager_can_reject_pending_leave_request(self):
        leave = LeaveRequest.objects.create(
            employee=self.employee, kind=LeaveRequest.Kind.ANNUAL,
            start_date=date(2026, 4, 1), end_date=date(2026, 4, 2),
            days=Decimal("2"),
        )
        manager = User.objects.create_user(username="hr_leave_manager", password="test-password")
        CompanyMembership.objects.create(
            user=manager, company=self.company, role="admin", all_branches=True,
        )
        self.client.force_authenticate(manager)
        response = self.client.post(f"/api/hr/leave-requests/{leave.pk}/reject/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        leave.refresh_from_db()
        self.assertEqual(leave.status, LeaveRequest.Status.REJECTED)
        self.assertEqual(leave.approved_by_id, manager.pk)

    def test_payroll_rule_set_rejects_malformed_numeric_configuration(self):
        response = self.client.post("/api/hr/payroll-rule-sets/", {
            "company": self.company.pk, "country": self.company.country,
            "name": "قواعد غير صالحة", "effective_from": "2026-01-01",
            "configuration": {"monthly_daily_divisor": "abc"},
            "is_active": True,
        }, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(PayrollRuleSet.objects.filter(company=self.company, name="قواعد غير صالحة").exists())

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

    def test_salary_history_records_previous_salary_and_updates_current_salary(self):
        response = self.client.post("/api/hr/salary-history/", {
            "employee": self.employee.pk,
            "effective_date": "2026-02-01",
            "new_salary": "11000",
            "change_type": "annual_raise",
            "reason": "زيادة سنوية",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.employee.refresh_from_db()
        self.assertEqual(response.data["previous_salary"], Decimal("10000"))
        self.assertEqual(self.employee.basic_salary, Decimal("11000"))

    def test_salary_history_records_cannot_be_edited_or_deleted(self):
        response = self.client.post("/api/hr/salary-history/", {
            "employee": self.employee.pk,
            "effective_date": "2026-04-01",
            "new_salary": "11500",
            "change_type": "adjustment",
            "reason": "تعديل موثق",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        history = EmployeeSalaryHistory.objects.get(pk=response.data["id"])
        history.reason = "تغيير غير موثق"
        with self.assertRaises(ValidationError):
            history.save()
        with self.assertRaises(ValidationError):
            history.delete()
        history.refresh_from_db()
        self.assertEqual(history.reason, "تعديل موثق")
        self.assertEqual(EmployeeSalaryHistory.objects.filter(employee=self.employee).count(), 1)

    def test_salary_history_rejects_negative_salary_and_duplicate_effective_date(self):
        negative = self.client.post("/api/hr/salary-history/", {
            "employee": self.employee.pk, "effective_date": "2026-02-01",
            "new_salary": "-1", "change_type": "adjustment",
        }, format="json")
        self.assertEqual(negative.status_code, 400)
        first = self.client.post("/api/hr/salary-history/", {
            "employee": self.employee.pk, "effective_date": "2026-03-01",
            "new_salary": "10500", "change_type": "annual_raise",
        }, format="json")
        duplicate = self.client.post("/api/hr/salary-history/", {
            "employee": self.employee.pk, "effective_date": "2026-03-01",
            "new_salary": "10800", "change_type": "adjustment",
        }, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(duplicate.status_code, 400)

    def test_future_salary_change_does_not_change_current_employee_salary(self):
        response = self.client.post("/api/hr/salary-history/", {
            "employee": self.employee.pk, "effective_date": "2099-01-01",
            "new_salary": "12000", "change_type": "annual_raise",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.basic_salary, Decimal("10000"))
        self.assertEqual(salary_for_date(self.employee, date(2098, 12, 31)), Decimal("10000"))
        self.assertEqual(salary_for_date(self.employee, date(2099, 1, 1)), Decimal("12000"))

    def test_backdated_salary_change_preserves_latest_salary_as_of_today(self):
        newer = self.client.post("/api/hr/salary-history/", {
            "employee": self.employee.pk, "effective_date": "2026-03-01",
            "new_salary": "12000", "change_type": "annual_raise",
        }, format="json")
        older = self.client.post("/api/hr/salary-history/", {
            "employee": self.employee.pk, "effective_date": "2026-02-01",
            "new_salary": "11000", "change_type": "annual_raise",
        }, format="json")
        self.assertEqual(newer.status_code, 201, newer.data)
        self.assertEqual(older.status_code, 201, older.data)
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.basic_salary, Decimal("12000"))
        self.assertEqual(salary_for_date(self.employee, date(2026, 11, 22)), Decimal("12000"))


    def test_leave_request_api_records_authenticated_creator(self):
        manager = User.objects.create_user(username="hr_leave_creator", password="test-password")
        CompanyMembership.objects.create(user=manager, company=self.company, role="admin", all_branches=True)
        self.client.force_authenticate(manager)
        created = self.client.post("/api/hr/leave-requests/", {
            "employee": self.employee.pk, "kind": LeaveRequest.Kind.ANNUAL,
            "start_date": "2026-10-01", "end_date": "2026-10-02", "days": "2",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        leave = LeaveRequest.objects.get(pk=created.data["id"])
        self.assertEqual(leave.created_by_id, manager.pk)

    def test_leave_request_creator_cannot_approve_or_reject_own_request(self):
        from audit.models import AuditEvent
        manager = User.objects.create_user(username="hr_leave_self_manager", password="test-password")
        CompanyMembership.objects.create(user=manager, company=self.company, role="admin", all_branches=True)
        leave = LeaveRequest.objects.create(
            employee=self.employee, kind=LeaveRequest.Kind.ANNUAL,
            start_date=date(2026, 5, 1), end_date=date(2026, 5, 2),
            days=Decimal("2"), created_by=manager,
        )
        self.client.force_authenticate(manager)
        approved = self.client.post(f"/api/hr/leave-requests/{leave.pk}/approve/", {}, format="json")
        self.assertEqual(approved.status_code, 403)
        leave.refresh_from_db()
        self.assertEqual(leave.status, LeaveRequest.Status.PENDING)
        rejected = self.client.post(f"/api/hr/leave-requests/{leave.pk}/reject/", {}, format="json")
        self.assertEqual(rejected.status_code, 403)
        leave.refresh_from_db()
        self.assertEqual(leave.status, LeaveRequest.Status.PENDING)
        self.assertFalse(AuditEvent.objects.filter(resource="hr.leave.self_approval_override", object_id=str(leave.pk)).exists())

    def test_superuser_self_approval_override_requires_reason_and_is_audited_for_leave(self):
        from audit.models import AuditEvent
        root = User.objects.create_superuser(username="hr_leave_root", email="root@example.test", password="test-password")
        leave = LeaveRequest.objects.create(
            employee=self.employee, kind=LeaveRequest.Kind.ANNUAL,
            start_date=date(2026, 6, 1), end_date=date(2026, 6, 2),
            days=Decimal("2"), created_by=root,
        )
        self.client.force_authenticate(root)
        missing_reason = self.client.post(f"/api/hr/leave-requests/{leave.pk}/approve/", {}, format="json")
        self.assertEqual(missing_reason.status_code, 403)
        allowed = self.client.post(
            f"/api/hr/leave-requests/{leave.pk}/approve/",
            {"self_approval_override_reason": "حالة طارئة موثقة من الإدارة العليا"},
            format="json",
        )
        self.assertEqual(allowed.status_code, 200, allowed.data)
        event = AuditEvent.objects.get(resource="hr.leave.self_approval_override", object_id=str(leave.pk))
        self.assertIn("حالة طارئة", event.metadata["reason"])

    def test_payroll_cycle_creator_cannot_approve_own_cycle(self):
        manager = User.objects.create_user(username="hr_payroll_self_manager", password="test-password")
        CompanyMembership.objects.create(user=manager, company=self.company, role="admin", all_branches=True)
        cycle = PayrollCycle.objects.create(
            company=self.company, period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31), payment_date=date(2026, 8, 1),
            status=PayrollCycle.Status.REVIEW, created_by=manager,
        )
        self.client.force_authenticate(manager)
        denied = self.client.post(f"/api/hr/payroll-cycles/{cycle.pk}/approve/", {}, format="json")
        self.assertEqual(denied.status_code, 403)
        cycle.refresh_from_db()
        self.assertEqual(cycle.status, PayrollCycle.Status.REVIEW)
        self.assertIsNone(cycle.approved_by_id)

    def test_superuser_payroll_self_approval_override_is_audited(self):
        from audit.models import AuditEvent
        root = User.objects.create_superuser(username="hr_payroll_root", email="payroll-root@example.test", password="test-password")
        cycle = PayrollCycle.objects.create(
            company=self.company, period_start=date(2026, 9, 1),
            period_end=date(2026, 9, 30), payment_date=date(2026, 10, 1),
            status=PayrollCycle.Status.REVIEW, created_by=root,
        )
        self.client.force_authenticate(root)
        denied = self.client.post(f"/api/hr/payroll-cycles/{cycle.pk}/approve/", {}, format="json")
        self.assertEqual(denied.status_code, 403)
        allowed = self.client.post(
            f"/api/hr/payroll-cycles/{cycle.pk}/approve/",
            {"self_approval_override_reason": "اعتماد استثنائي بسبب تعذر حضور المدير المالي"},
            format="json",
        )
        self.assertEqual(allowed.status_code, 200, allowed.data)
        event = AuditEvent.objects.get(resource="hr.payroll.self_approval_override", object_id=str(cycle.pk))
        self.assertIn("تعذر حضور", event.metadata["reason"])


class PayrollTaxPostingTests(TestCase):
    def test_progressive_tax_without_component_account_uses_company_tax_payable_role(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        company = Company.objects.create(
            code="HR-TAX", name_ar="شركة اختبار ضريبة المرتبات", country="EG",
            activity="commercial", base_currency=currency,
        )
        branch = Branch.objects.create(
            company=company, code="HQ", name_ar="المركز الرئيسي", is_head_office=True,
        )
        FiscalYear.objects.create(
            company=company, name="2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31),
        )
        actor = User.objects.create_user(username="payroll-tax-poster", password="test-password")
        employee = Employee.objects.create(
            company=company, code="EMP-TAX", full_name_ar="موظف ضريبة",
            branch=branch, hire_date=date(2026, 1, 1), basic_salary=Decimal("1000"), currency=currency,
        )
        cycle = PayrollCycle.objects.create(
            company=company, period_start=date(2026, 1, 1), period_end=date(2026, 1, 31),
            payment_date=date(2026, 2, 1), status=PayrollCycle.Status.APPROVED,
        )
        accounts = {
            "payroll_expense": ("6100", "مصروف المرتبات", ChartOfAccount.Nature.EXPENSE),
            "payroll_payable": ("2200", "مرتبات مستحقة", ChartOfAccount.Nature.LIABILITY),
            "payroll_tax_payable": ("2210", "ضريبة المرتبات المستحقة", ChartOfAccount.Nature.LIABILITY),
        }
        for role, (code, name, nature) in accounts.items():
            account = ChartOfAccount.objects.create(company=company, code=code, name_ar=name, nature=nature)
            AccountRole.objects.create(company=company, role=role, account=account)
        tax_account = AccountRole.objects.get(company=company, role="payroll_tax_payable").account
        tax_component = PayrollComponent.objects.create(
            company=company, code="TAX", name_ar="ضريبة المرتبات",
            kind=PayrollComponent.Kind.DEDUCTION,
            method=PayrollComponent.Method.PROGRESSIVE_TAX,
        )
        result = PayrollResult.objects.create(
            cycle=cycle, employee=employee, basic_salary_snapshot=Decimal("1000"),
            earnings_total=Decimal("1000"), employee_deductions_total=Decimal("100"),
            net_pay=Decimal("900"), company_cost=Decimal("1000"),
        )
        PayrollResultLine.objects.create(
            result=result, code_snapshot="BASIC", name_snapshot="الراتب الأساسي",
            kind_snapshot="earning", amount=Decimal("1000"),
        )
        PayrollResultLine.objects.create(
            result=result, component=tax_component, code_snapshot="TAX",
            name_snapshot="ضريبة المرتبات", kind_snapshot="deduction", amount=Decimal("100"),
        )

        posted = post_payroll_cycle(cycle.pk, actor)
        self.assertEqual(posted["status"], PayrollCycle.Status.POSTED)
        tax_line = JournalEntry.objects.get(pk=posted["postings"][0]["journal_entry_id"]).lines.get(account=tax_account)
        self.assertEqual(tax_line.credit, Decimal("100.0000"))
        self.assertEqual(tax_line.debit, Decimal("0.0000"))


    def test_prepared_payroll_payment_cannot_be_changed_and_completion_rechecks_amount(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        company = Company.objects.create(
            code="HR-PAY-GUARD", name_ar="شركة حماية صرف المرتبات", country="EG",
            activity="commercial", base_currency=currency,
        )
        branch = Branch.objects.create(
            company=company, code="HQ", name_ar="المركز الرئيسي", is_head_office=True,
        )
        year = FiscalYear.objects.create(
            company=company, name="2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31),
        )
        actor = User.objects.create_user(username="payroll-payment-guard", password="test-password")
        employee = Employee.objects.create(
            company=company, code="EMP-PAY", full_name_ar="موظف صرف",
            branch=branch, hire_date=date(2026, 1, 1), basic_salary=Decimal("1000"), currency=currency,
        )
        cycle = PayrollCycle.objects.create(
            company=company, period_start=date(2026, 1, 1), period_end=date(2026, 1, 31),
            payment_date=date(2026, 2, 1), status=PayrollCycle.Status.APPROVED,
        )
        role_accounts = {
            "payroll_expense": ("6101", "مصروف المرتبات", ChartOfAccount.Nature.EXPENSE),
            "payroll_payable": ("2201", "مرتبات مستحقة", ChartOfAccount.Nature.LIABILITY),
        }
        for role, (code, name, nature) in role_accounts.items():
            account = ChartOfAccount.objects.create(company=company, code=code, name_ar=name, nature=nature)
            AccountRole.objects.create(company=company, role=role, account=account)
        PayrollResult.objects.create(
            cycle=cycle, employee=employee, basic_salary_snapshot=Decimal("1000"),
            earnings_total=Decimal("1000"), employee_deductions_total=Decimal("100"),
            net_pay=Decimal("900"), company_cost=Decimal("1000"),
        )
        result = cycle.results.get()
        PayrollResultLine.objects.create(
            result=result, code_snapshot="BASIC", name_snapshot="الراتب الأساسي",
            kind_snapshot="earning", amount=Decimal("1000"),
        )
        deduction_component = PayrollComponent.objects.create(
            company=company, code="OTHER-DEDUCTION", name_ar="خصم",
            kind=PayrollComponent.Kind.DEDUCTION, method=PayrollComponent.Method.FIXED,
            payable_account=AccountRole.objects.get(company=company, role="payroll_payable").account,
        )
        PayrollResultLine.objects.create(
            result=result, component=deduction_component, code_snapshot="OTHER-DEDUCTION",
            name_snapshot="خصم", kind_snapshot="deduction", amount=Decimal("100"),
        )
        post_payroll_cycle(cycle.pk, actor)
        cycle.refresh_from_db()
        cycle.status = PayrollCycle.Status.PAYMENT_PENDING
        cycle.save(update_fields=("status", "updated_at"))
        posting = cycle.postings.get()
        bank_gl = ChartOfAccount.objects.create(
            company=company, code="1111", name_ar="بنك المرتبات", nature=ChartOfAccount.Nature.ASSET,
        )
        bank = TreasuryAccount.objects.create(
            company=company, branch=branch, account=bank_gl, kind=TreasuryAccount.Kind.BANK,
            code="BANK-PAY-GUARD", name_ar="بنك المرتبات", currency_code="EGP",
            opening_balance=Decimal("5000"),
        )
        payroll_payable = AccountRole.objects.get(company=company, role="payroll_payable").account
        document = TreasuryTransaction.objects.create(
            company=company, branch=branch, fiscal_year=year, source_account=bank,
            counter_account=payroll_payable, kind=TreasuryTransaction.Kind.PAYMENT,
            document_number="PAY-GUARD-1", transaction_date=date(2026, 2, 1),
            amount=Decimal("900"), description="صرف مرتبات",
        )
        posting.payment_treasury_transaction = document
        posting.save(update_fields=("payment_treasury_transaction",))

        document.amount = Decimal("899")
        with self.assertRaises(ValidationError):
            document.save()
        TreasuryTransaction.objects.filter(pk=document.pk).update(amount=Decimal("899"))
        with self.assertRaises(ValidationError):
            complete_payroll_payment(cycle.pk, actor)
        cycle.refresh_from_db()
        self.assertEqual(cycle.status, PayrollCycle.Status.PAYMENT_PENDING)
        self.assertIsNone(posting.payment_journal_entry_id)
