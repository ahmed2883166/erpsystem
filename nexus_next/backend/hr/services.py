from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum

from .models import AttendanceRecord, Employee, EmployeeComponent, LoanInstallment, PayrollCycle, PayrollResult, PayrollResultLine


CENT = Decimal("0.01")


def money(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def salary_for_date(employee, target_date):
    history = employee.salary_history.filter(effective_date__lte=target_date).order_by("-effective_date", "-id").first()
    if history:
        return history.new_salary
    first = employee.salary_history.order_by("effective_date", "id").first()
    return first.previous_salary if first else employee.basic_salary


def calculate_component(component, assigned_value, basic_salary, payable_days, overtime_hours, daily_divisor, friday_days):
    value = assigned_value if assigned_value is not None else component.default_value
    if component.method == component.Method.FIXED:
        amount = value
    elif component.method == component.Method.PERCENT_BASIC:
        amount = basic_salary * value / Decimal("100")
    elif component.method == component.Method.PER_DAY:
        amount = value * payable_days
    elif component.method == component.Method.PER_HOUR:
        amount = value * overtime_hours
    elif component.method == component.Method.FRIDAY_DOUBLE_DAY:
        amount = basic_salary / daily_divisor * Decimal("2") * friday_days
    else:
        amount = value
    return money(amount)


@transaction.atomic
def calculate_payroll_cycle(cycle_id, actor=None):
    cycle = PayrollCycle.objects.select_for_update().select_related("company", "rule_set").get(pk=cycle_id)
    if cycle.status not in (PayrollCycle.Status.DRAFT, PayrollCycle.Status.CALCULATED, PayrollCycle.Status.REVIEW):
        raise ValidationError("لا يمكن إعادة احتساب دورة معتمدة أو مرحلة أو مصروفة.")
    employees = Employee.objects.filter(
        company=cycle.company,
        hire_date__lte=cycle.period_end,
        status__in=(Employee.Status.ACTIVE, Employee.Status.LEAVE, Employee.Status.UNPAID_LEAVE),
    ).exclude(termination_date__lt=cycle.period_start).select_related("currency", "branch", "department")
    PayrollResultLine.objects.filter(result__cycle=cycle).delete()
    cycle.results.all().delete()
    totals = {"earnings": Decimal("0"), "employee_deductions": Decimal("0"), "employer_share": Decimal("0"), "net": Decimal("0"), "company_cost": Decimal("0")}
    for employee in employees:
        assignments = EmployeeComponent.objects.filter(
            employee=employee, is_active=True, effective_from__lte=cycle.period_end
        ).filter(models_q_end(cycle.period_start)).select_related("component")
        employment_start = max(cycle.period_start, employee.hire_date)
        employment_end = min(cycle.period_end, employee.termination_date or cycle.period_end)
        calendar_days = Decimal(max(0, (employment_end - employment_start).days + 1))
        full_calendar_month = (
            cycle.period_start.day == 1
            and cycle.period_end.month == cycle.period_start.month
            and cycle.period_end.year == cycle.period_start.year
            and cycle.period_end.day == (cycle.period_end.replace(day=28) + __import__("datetime").timedelta(days=4)).replace(day=1).day - 1
        )
        attendance = AttendanceRecord.objects.filter(employee=employee, work_date__range=(employment_start, employment_end))
        attendance_count = attendance.count()
        base_payable_days = Decimal("30") if full_calendar_month and employment_start == cycle.period_start and employment_end == cycle.period_end else min(calendar_days, Decimal("30"))
        if attendance_count:
            absent_days = attendance.filter(status__in=(AttendanceRecord.Status.ABSENT, AttendanceRecord.Status.UNPAID_LEAVE)).count()
            payable_days = max(Decimal("0"), base_payable_days - Decimal(absent_days))
        elif employee.status == Employee.Status.UNPAID_LEAVE:
            payable_days = Decimal("0")
        else:
            payable_days = base_payable_days
        daily_divisor = Decimal(str((cycle.rule_set.configuration if cycle.rule_set_id else {}).get("monthly_daily_divisor", 30)))
        if daily_divisor <= 0:
            raise ValidationError("مقسم الراتب اليومي يجب أن يكون أكبر من صفر.")
        effective_basic_salary = salary_for_date(employee, cycle.period_end)
        basic_amount = money(effective_basic_salary * payable_days / daily_divisor)
        earnings = basic_amount
        overtime_hours = attendance.aggregate(total=Sum("overtime_hours"))["total"] or Decimal("0")
        friday_days = Decimal(attendance.filter(friday_worked=True).count())
        deductions = Decimal("0")
        employer_share = Decimal("0")
        lines = [(None, "BASIC", "الراتب الأساسي", "earning", basic_amount, False, {"method": "monthly_prorated", "payable_days": str(payable_days), "daily_divisor": str(daily_divisor)})]
        for assignment in assignments:
            component = assignment.component
            amount = calculate_component(component, assignment.value, effective_basic_salary, payable_days, overtime_hours, daily_divisor, friday_days)
            if component.kind == component.Kind.EARNING:
                earnings += amount
            elif component.kind == component.Kind.DEDUCTION:
                deductions += amount
            else:
                employer_share += amount
            lines.append((component, component.code, component.name_ar, component.kind, amount, component.kind == component.Kind.EMPLOYER_SHARE, {"method": component.method, "assigned_value": str(assignment.value), "overtime_hours": str(overtime_hours)}))
        installments = LoanInstallment.objects.filter(
            loan__employee=employee,
            due_date__range=(cycle.period_start, cycle.period_end),
            status__in=(LoanInstallment.Status.DUE, LoanInstallment.Status.PARTIAL),
        ).select_related("loan")
        for installment in installments:
            remaining = money(installment.amount - installment.paid_amount)
            if remaining > 0:
                deductions += remaining
                lines.append((
                    None, f"LOAN-{installment.loan_id}-{installment.pk}",
                    f"قسط سلفة {installment.loan.reference}", "deduction", remaining, False,
                    {"loan_id": installment.loan_id, "installment_id": installment.pk, "due_date": installment.due_date.isoformat()},
                ))
        result = PayrollResult.objects.create(
            cycle=cycle, employee=employee, basic_salary_snapshot=effective_basic_salary,
            earnings_total=money(earnings), employee_deductions_total=money(deductions),
            employer_share_total=money(employer_share),
            employer_share_display_addition=money(employer_share),
            employer_share_display_offset=money(employer_share),
            net_pay=money(earnings - deductions),
            company_cost=money(earnings + employer_share),
            calculation_snapshot={
                "period_start": cycle.period_start.isoformat(), "period_end": cycle.period_end.isoformat(),
                "country": cycle.company.country, "rule_set_id": cycle.rule_set_id,
                "basic_salary": str(effective_basic_salary), "payable_days": str(payable_days),
            },
        )
        for component, code, name, kind, amount, display_only, snapshot in lines:
            PayrollResultLine.objects.create(
                result=result, component=component, code_snapshot=code, name_snapshot=name, kind_snapshot=kind,
                amount=amount, display_only=display_only, calculation_snapshot=snapshot,
            )
        if employer_share:
            PayrollResultLine.objects.create(
                result=result, code_snapshot="EMPLOYER_SHARE_DISPLAY",
                name_snapshot="حصة صاحب العمل - إضافة للعرض", kind_snapshot="earning",
                amount=money(employer_share), display_only=True,
                calculation_snapshot={"paired_display_line": "EMPLOYER_SHARE_OFFSET"},
            )
            PayrollResultLine.objects.create(
                result=result, code_snapshot="EMPLOYER_SHARE_OFFSET",
                name_snapshot="حصة صاحب العمل - تسوية عرضية", kind_snapshot="deduction",
                amount=money(employer_share), display_only=True,
                calculation_snapshot={"paired_display_line": "EMPLOYER_SHARE_DISPLAY", "excluded_from_net": True},
            )
        totals["earnings"] += earnings
        totals["employee_deductions"] += deductions
        totals["employer_share"] += employer_share
        totals["net"] += earnings - deductions
        totals["company_cost"] += earnings + employer_share
    cycle.status = PayrollCycle.Status.CALCULATED
    cycle.save(update_fields=("status", "updated_at"))
    return {"cycle_id": cycle.pk, "employee_count": len(employees), **{key: str(money(value)) for key, value in totals.items()}}


def models_q_end(start_date):
    from django.db.models import Q
    return Q(effective_to__isnull=True) | Q(effective_to__gte=start_date)
