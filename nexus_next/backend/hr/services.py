from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Employee, EmployeeComponent, PayrollCycle, PayrollResult, PayrollResultLine


CENT = Decimal("0.01")


def money(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def calculate_component(component, assigned_value, basic_salary, payable_days, overtime_hours):
    value = assigned_value if assigned_value is not None else component.default_value
    if component.method == component.Method.FIXED:
        amount = value
    elif component.method == component.Method.PERCENT_BASIC:
        amount = basic_salary * value / Decimal("100")
    elif component.method == component.Method.PER_DAY:
        amount = value * payable_days
    elif component.method == component.Method.PER_HOUR:
        amount = value * overtime_hours
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
    cycle.results.all().delete()
    totals = {"earnings": Decimal("0"), "employee_deductions": Decimal("0"), "employer_share": Decimal("0"), "net": Decimal("0"), "company_cost": Decimal("0")}
    for employee in employees:
        assignments = EmployeeComponent.objects.filter(
            employee=employee, is_active=True, effective_from__lte=cycle.period_end
        ).filter(models_q_end(cycle.period_start)).select_related("component")
        payable_days = Decimal((cycle.period_end - cycle.period_start).days + 1)
        if employee.status == Employee.Status.UNPAID_LEAVE:
            payable_days = Decimal("0")
        earnings = Decimal("0")
        deductions = Decimal("0")
        employer_share = Decimal("0")
        snapshots = []
        lines = []
        for assignment in assignments:
            component = assignment.component
            amount = calculate_component(component, assignment.value, employee.basic_salary, payable_days, Decimal("0"))
            if component.kind == component.Kind.EARNING:
                earnings += amount
            elif component.kind == component.Kind.DEDUCTION:
                deductions += amount
            else:
                employer_share += amount
            lines.append((component, amount, False, {"method": component.method, "assigned_value": str(assignment.value)}))
        result = PayrollResult.objects.create(
            cycle=cycle, employee=employee, basic_salary_snapshot=employee.basic_salary,
            earnings_total=money(earnings), employee_deductions_total=money(deductions),
            employer_share_total=money(employer_share),
            employer_share_display_addition=money(employer_share),
            employer_share_display_offset=money(employer_share),
            net_pay=money(earnings - deductions),
            company_cost=money(earnings + employer_share),
            calculation_snapshot={
                "period_start": cycle.period_start.isoformat(), "period_end": cycle.period_end.isoformat(),
                "country": cycle.company.country, "rule_set_id": cycle.rule_set_id,
                "basic_salary": str(employee.basic_salary), "payable_days": str(payable_days),
            },
        )
        for component, amount, display_only, snapshot in lines:
            PayrollResultLine.objects.create(
                result=result, component=component, code_snapshot=component.code,
                name_snapshot=component.name_ar, kind_snapshot=component.kind,
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
