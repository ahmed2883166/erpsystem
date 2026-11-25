"""خدمات احتساب المرتبات ومكوناتها والحضور والسلف وتجهيز وترحيل وصرف المستحقات."""

from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum

from accounting.models import JournalEntry, JournalLine
from accounting.services import get_account_for_role, post_journal
from companies.models import FiscalYear
from approvals.models import FinancialApprovalRequest
from approvals.services import refresh_financial_approval
from treasury.models import TreasuryAccount, TreasuryTransaction
from treasury.services import post_treasury_transaction

from .models import AttendanceRecord, Employee, EmployeeComponent, EmployeeLoan, LoanInstallment, LeaveRequest, PayrollCycle, PayrollPosting, PayrollResult, PayrollResultLine


CENT = Decimal("0.01")

# Egypt salary-tax tables under Law 7/2024, effective for salaries from 2024-03-01.
# The income ceiling selects the applicable column; lower brackets disappear as income rises.
EGYPT_2026_TAX_BRACKET_GROUPS = [
    {"income_max": "600000", "brackets": [
        {"up_to": "40000", "rate": "0"}, {"up_to": "55000", "rate": "10"},
        {"up_to": "70000", "rate": "15"}, {"up_to": "200000", "rate": "20"},
        {"up_to": "400000", "rate": "22.5"}, {"up_to": None, "rate": "25"},
    ]},
    {"income_max": "700000", "brackets": [
        {"up_to": "55000", "rate": "10"}, {"up_to": "70000", "rate": "15"},
        {"up_to": "200000", "rate": "20"}, {"up_to": "400000", "rate": "22.5"},
        {"up_to": None, "rate": "25"},
    ]},
    {"income_max": "800000", "brackets": [
        {"up_to": "70000", "rate": "15"}, {"up_to": "200000", "rate": "20"},
        {"up_to": "400000", "rate": "22.5"}, {"up_to": None, "rate": "25"},
    ]},
    {"income_max": "900000", "brackets": [
        {"up_to": "200000", "rate": "20"}, {"up_to": "400000", "rate": "22.5"},
        {"up_to": None, "rate": "25"},
    ]},
    {"income_max": "1200000", "brackets": [
        {"up_to": "400000", "rate": "22.5"}, {"up_to": None, "rate": "25"},
    ]},
    {"income_max": None, "brackets": [
        {"up_to": "1200000", "rate": "25"}, {"up_to": None, "rate": "27.5"},
    ]},
]


def money(value):
    """تحويل القيمة إلى مبلغ عشري آمن للاستخدام في الحسابات المالية."""
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def salary_for_date(employee, target_date):
    """استخراج الراتب الأساسي الساري للموظف في التاريخ المحدد."""
    history = employee.salary_history.filter(effective_date__lte=target_date).order_by("-effective_date", "-id").first()
    if history:
        return history.new_salary
    first = employee.salary_history.order_by("effective_date", "id").first()
    return first.previous_salary if first else employee.basic_salary


def _config_decimal(section, key):
    """قراءة قيمة عشرية من إعدادات المرتبات مع معالجة القيمة الافتراضية."""
    if key not in section or section[key] in (None, ""):
        raise ValidationError(f"إعداد القاعدة القانونية ({key}) غير مكتمل.")
    try:
        return Decimal(str(section[key]))
    except Exception as exc:
        raise ValidationError(f"قيمة إعداد القاعدة القانونية ({key}) غير صحيحة.") from exc


def calculate_component(
    component, assigned_value, basic_salary, payable_days, overtime_hours,
    daily_divisor, friday_days, rule_configuration=None, taxable_base=None, insurance_base=None,
):
    """احتساب قيمة عنصر المرتب وفق نوعه وقواعده ومدخلاته."""
    value = assigned_value if assigned_value is not None else component.default_value
    rule_configuration = rule_configuration or {}
    taxable_base = taxable_base if taxable_base is not None else basic_salary
    insurance_base = insurance_base if insurance_base is not None else basic_salary
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
    elif component.method in (component.Method.EMPLOYEE_INSURANCE, component.Method.EMPLOYER_INSURANCE):
        insurance = rule_configuration.get("insurance") or {}
        minimum = _config_decimal(insurance, "min_base")
        maximum = _config_decimal(insurance, "max_base")
        rate_key = "employee_rate" if component.method == component.Method.EMPLOYEE_INSURANCE else "employer_rate"
        rate = _config_decimal(insurance, rate_key)
        if minimum < 0 or maximum < minimum or rate < 0 or rate > 100:
            raise ValidationError("إعدادات التأمينات غير صحيحة؛ راجع الحد الأدنى والأقصى والنسبة.")
        contribution_base = min(max(insurance_base, minimum), maximum)
        amount = contribution_base * rate / Decimal("100")
    elif component.method == component.Method.PROGRESSIVE_TAX:
        tax = rule_configuration.get("tax") or {}
        exemption = _config_decimal(tax, "annual_personal_exemption")
        annual_taxable = max(Decimal("0"), taxable_base * Decimal("12") - exemption)
        annual_taxable = (annual_taxable / Decimal("10")).to_integral_value(rounding=ROUND_DOWN) * Decimal("10")
        bracket_groups = tax.get("annual_bracket_groups")
        if isinstance(bracket_groups, list) and bracket_groups:
            selected_group = next((group for group in bracket_groups
                if group.get("income_max") in (None, "") or annual_taxable <= Decimal(str(group["income_max"]))), None)
            brackets = selected_group.get("brackets") if selected_group else None
        else:
            brackets = tax.get("annual_brackets")
        if exemption < 0 or not isinstance(brackets, list) or not brackets:
            raise ValidationError("شرائح ضريبة المرتبات غير مكتملة أو لا تغطي الوعاء السنوي.")
        lower = Decimal("0")
        annual_tax = Decimal("0")
        has_open_ended_bracket = False
        covered = annual_taxable == 0
        for bracket in brackets:
            if not isinstance(bracket, dict) or "rate" not in bracket:
                raise ValidationError("تعريف شريحة الضريبة غير صحيح.")
            rate = Decimal(str(bracket["rate"]))
            if rate < 0 or rate > 100:
                raise ValidationError("نسبة شريحة الضريبة يجب أن تكون بين صفر و100.")
            upper_value = bracket.get("up_to")
            if upper_value in (None, ""):
                has_open_ended_bracket = True
                annual_tax += max(Decimal("0"), annual_taxable - lower) * rate / Decimal("100")
                break
            upper = Decimal(str(upper_value))
            if upper <= lower:
                raise ValidationError("حدود شرائح الضريبة يجب أن تكون تصاعدية.")
            annual_tax += max(Decimal("0"), min(annual_taxable, upper) - lower) * rate / Decimal("100")
            if annual_taxable <= upper:
                covered = True
                break
            lower = upper
        if not has_open_ended_bracket and not covered and annual_taxable > lower:
            raise ValidationError("شرائح الضريبة لا تغطي كامل الوعاء السنوي.")
        amount = annual_tax / Decimal("12")
    else:
        amount = value
    return money(amount)


@transaction.atomic
def calculate_payroll_cycle(cycle_id, actor=None):
    """احتساب دورة المرتبات ومكوناتها واستقطاعاتها للموظفين المشمولين."""
    cycle = PayrollCycle.objects.select_for_update(of=("self",)).select_related("company", "rule_set").get(pk=cycle_id)
    if cycle.status not in (PayrollCycle.Status.DRAFT, PayrollCycle.Status.CALCULATED, PayrollCycle.Status.REVIEW):
        raise ValidationError("لا يمكن إعادة احتساب دورة معتمدة أو مرحلة أو مصروفة.")
    if cycle.rule_set_id:
        if not cycle.rule_set.is_active or cycle.rule_set.country != cycle.company.country:
            raise ValidationError("مجموعة القواعد غير نشطة أو لا تطابق بلد الشركة.")
        if cycle.rule_set.effective_from > cycle.period_start or (
            cycle.rule_set.effective_to and cycle.rule_set.effective_to < cycle.period_end
        ):
            raise ValidationError("مجموعة القواعد لا تغطي كامل فترة المرتبات.")
    if PayrollCycle.objects.filter(
        company=cycle.company, period_start__lte=cycle.period_end, period_end__gte=cycle.period_start
    ).exclude(pk=cycle.pk).exists():
        raise ValidationError("توجد دورة مرتبات أخرى تتداخل مع هذه الفترة؛ راجع الدورات قبل الاحتساب.")
    employees = Employee.objects.filter(
        company=cycle.company,
        hire_date__lte=cycle.period_end,
    ).filter(
        Q(status__in=(Employee.Status.ACTIVE, Employee.Status.LEAVE, Employee.Status.UNPAID_LEAVE))
        | Q(status__in=(Employee.Status.RESIGNED, Employee.Status.RETIRED), termination_date__gte=cycle.period_start)
    ).select_related("currency", "branch", "department")
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
        last_day_of_month = ((cycle.period_end.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)).day
        full_calendar_month = (
            cycle.period_start.day == 1
            and cycle.period_end.month == cycle.period_start.month
            and cycle.period_end.year == cycle.period_start.year
            and cycle.period_end.day == last_day_of_month
        )
        attendance = AttendanceRecord.objects.filter(employee=employee, work_date__range=(employment_start, employment_end))
        attendance_count = attendance.count()
        base_payable_days = Decimal("30") if full_calendar_month and employment_start == cycle.period_start and employment_end == cycle.period_end else min(calendar_days, Decimal("30"))
        unpaid_dates = set(attendance.filter(
            status__in=(AttendanceRecord.Status.ABSENT, AttendanceRecord.Status.UNPAID_LEAVE)
        ).values_list("work_date", flat=True))
        approved_unpaid = LeaveRequest.objects.filter(
            employee=employee, status=LeaveRequest.Status.APPROVED, kind=LeaveRequest.Kind.UNPAID,
            start_date__lte=employment_end, end_date__gte=employment_start,
        ).values_list("start_date", "end_date")
        for leave_start, leave_end in approved_unpaid:
            day = max(leave_start, employment_start)
            final_day = min(leave_end, employment_end)
            while day <= final_day:
                unpaid_dates.add(day)
                day += timedelta(days=1)
        if not attendance_count and employee.status == Employee.Status.UNPAID_LEAVE and not approved_unpaid:
            payable_days = Decimal("0")
        else:
            payable_days = max(Decimal("0"), base_payable_days - Decimal(len(unpaid_dates)))
        try:
            daily_divisor = Decimal(str((cycle.rule_set.configuration if cycle.rule_set_id else {}).get("monthly_daily_divisor", 30)))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValidationError("مقسم الراتب اليومي في مجموعة القواعد غير صالح.") from exc
        if not daily_divisor.is_finite() or daily_divisor <= 0:
            raise ValidationError("مقسم الراتب اليومي يجب أن يكون أكبر من صفر.")
        effective_basic_salary = salary_for_date(employee, cycle.period_end)
        basic_amount = money(effective_basic_salary * payable_days / daily_divisor)
        earnings = basic_amount
        overtime_hours = attendance.aggregate(total=Sum("overtime_hours"))["total"] or Decimal("0")
        friday_days = Decimal(attendance.filter(friday_worked=True).count())
        deductions = Decimal("0")
        employer_share = Decimal("0")
        taxable_base = basic_amount
        insurance_base = basic_amount
        lines = [(None, "BASIC", "الراتب الأساسي", "earning", basic_amount, False, {"method": "monthly_prorated", "payable_days": str(payable_days), "daily_divisor": str(daily_divisor)})]
        rule_configuration = cycle.rule_set.configuration.copy() if cycle.rule_set_id else {}
        if cycle.company.country == "EG" and cycle.period_start.year == 2026:
            rule_configuration["tax"] = (rule_configuration.get("tax") or {}).copy()
            rule_configuration["tax"].setdefault("annual_personal_exemption", "20000")
            rule_configuration["tax"].setdefault("annual_bracket_groups", EGYPT_2026_TAX_BRACKET_GROUPS)

        def safe_component_amount(*args):
            """تنفيذ مسؤولية الدالة safe_component_amount ضمن وحدة services.py مع الحفاظ على قواعد التحقق والتعامل مع البيانات."""
            try:
                return calculate_component(*args)
            except (InvalidOperation, TypeError, ValueError, KeyError, IndexError) as exc:
                raise ValidationError("إعدادات أحد مكونات المرتبات غير صالحة؛ راجع مجموعة القواعد السنوية.") from exc

        statutory_assignments = []
        for assignment in assignments:
            component = assignment.component
            if component.method in (
                component.Method.EMPLOYEE_INSURANCE,
                component.Method.EMPLOYER_INSURANCE,
                component.Method.PROGRESSIVE_TAX,
            ):
                statutory_assignments.append(assignment)
                continue
            amount = safe_component_amount(
                component, assignment.value, effective_basic_salary, payable_days,
                overtime_hours, daily_divisor, friday_days, rule_configuration,
                taxable_base, insurance_base,
            )
            if component.kind == component.Kind.EARNING:
                earnings += amount
                if component.affects_taxable_base:
                    taxable_base += amount
                if component.affects_insurance_base:
                    insurance_base += amount
            elif component.kind == component.Kind.DEDUCTION:
                deductions += amount
            else:
                employer_share += amount
            lines.append((component, component.code, component.name_ar, component.kind, amount, component.kind == component.Kind.EMPLOYER_SHARE, {"method": component.method, "assigned_value": str(assignment.value), "overtime_hours": str(overtime_hours)}))
        # Employee social-insurance contributions reduce the Egyptian annual
        # salary-tax base. Calculate them before progressive tax regardless of
        # the order in which statutory components were assigned to the employee.
        taxable_base_before_employee_insurance = taxable_base
        for assignment in statutory_assignments:
            component = assignment.component
            if component.method != component.Method.EMPLOYEE_INSURANCE:
                continue
            amount = safe_component_amount(
                component, assignment.value, effective_basic_salary, payable_days,
                overtime_hours, daily_divisor, friday_days, rule_configuration,
                taxable_base, insurance_base,
            )
            deductions += amount
            taxable_base = max(Decimal("0"), taxable_base - amount)
            lines.append((component, component.code, component.name_ar, component.kind, amount, False, {
                "method": component.method,
                "taxable_base_before_employee_insurance": str(taxable_base + amount),
                "taxable_base": str(taxable_base),
                "insurance_base": str(insurance_base),
            }))

        for assignment in statutory_assignments:
            component = assignment.component
            if component.method == component.Method.EMPLOYEE_INSURANCE:
                continue
            amount = safe_component_amount(
                component, assignment.value, effective_basic_salary, payable_days,
                overtime_hours, daily_divisor, friday_days, rule_configuration,
                taxable_base, insurance_base,
            )
            if component.method == component.Method.PROGRESSIVE_TAX:
                deductions += amount
            else:
                employer_share += amount
            lines.append((component, component.code, component.name_ar, component.kind, amount, component.kind == component.Kind.EMPLOYER_SHARE, {
                "method": component.method,
                "taxable_base_before_employee_insurance": str(taxable_base_before_employee_insurance),
                "taxable_base": str(taxable_base),
                "insurance_base": str(insurance_base),
            }))
        installments = LoanInstallment.objects.filter(
            loan__employee=employee,
            loan__status=EmployeeLoan.Status.ACTIVE,
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

@transaction.atomic
def post_payroll_cycle(cycle_id, actor=None):
    """ترحيل مستحقات دورة المرتبات إلى القيود والحسابات المرتبطة."""
    cycle = PayrollCycle.objects.select_for_update().select_related("company").get(pk=cycle_id)
    if cycle.status != PayrollCycle.Status.APPROVED:
        raise ValidationError("يجب اعتماد دورة المرتبات قبل الترحيل المحاسبي.")
    if cycle.postings.exists() or cycle.journal_entry_id:
        raise ValidationError("تم ترحيل هذه الدورة أو جزء منها من قبل.")
    if not cycle.results.exists():
        raise ValidationError("لا توجد نتائج احتساب لترحيلها.")
    fiscal_year = FiscalYear.objects.filter(
        company=cycle.company,
        start_date__lte=cycle.period_end,
        end_date__gte=cycle.period_end,
    ).first()
    if not fiscal_year or fiscal_year.status != FiscalYear.Status.OPEN:
        raise ValidationError("لا توجد سنة مالية مفتوحة تغطي تاريخ صرف المرتبات.")

    payroll_expense = get_account_for_role(cycle.company, "payroll_expense")
    payroll_payable = get_account_for_role(cycle.company, "payroll_payable")
    branch_ids = list(cycle.results.values_list("employee__branch_id", flat=True).distinct())
    if not branch_ids or None in branch_ids:
        raise ValidationError("يجب إسناد كل موظفي دورة المرتبات إلى فرع قبل الترحيل.")
    posted = []

    def validate_account(account, nature, description):
        """التحقق من صلاحية الحساب وطبيعته قبل استخدامه في قيد المرتبات."""
        if not account or account.company_id != cycle.company_id or not account.is_active or account.is_group or not account.allow_posting:
            raise ValidationError(f"الحساب المرتبط بـ {description} غير صالح للترحيل.")
        if account.nature != nature:
            raise ValidationError(f"طبيعة حساب {description} غير صحيحة.")
        return account

    for branch_id in branch_ids:
        from companies.models import Branch
        branch = Branch.objects.get(pk=branch_id, company=cycle.company)
        results = cycle.results.filter(employee__branch=branch).select_related("employee", "employee__cost_center").prefetch_related("lines__component")
        entry = JournalEntry.objects.create(
            company=cycle.company,
            branch=branch,
            fiscal_year=fiscal_year,
            entry_number=f"PAY{cycle.pk}-{branch.pk}",
            entry_date=cycle.period_end,
            description=f"استحقاق مرتبات الفترة {cycle.period_start} إلى {cycle.period_end}",
            source_type=JournalEntry.Source.PAYROLL,
            created_by=actor,
        )
        line_count = 0

        def add_line(account, debit, credit, description, cost_center=None, entry=entry):
            """تنفيذ مسؤولية الدالة add_line ضمن وحدة services.py مع الحفاظ على قواعد التحقق والتعامل مع البيانات."""
            nonlocal line_count
            debit = money(debit)
            credit = money(credit)
            if debit <= 0 and credit <= 0:
                return
            expected_nature = "expense" if debit > 0 and credit == 0 else (account.nature if account else "liability")
            validate_account(account, expected_nature, description)
            if cost_center and cost_center.company_id != cycle.company_id:
                raise ValidationError("مركز تكلفة المرتبات لا يتبع نفس الشركة.")
            JournalLine.objects.create(
                entry=entry, account=account, debit=debit, credit=credit,
                description=description[:300], cost_center=cost_center,
            )
            line_count += 1

        for result in results:
            if result.net_pay < 0:
                raise ValidationError(f"صافي المرتب سالب للموظف {result.employee.code}؛ راجع الاستقطاعات.")
            for line in result.lines.all():
                component = line.component
                if line.code_snapshot in ("EMPLOYER_SHARE_DISPLAY", "EMPLOYER_SHARE_OFFSET"):
                    continue
                if line.kind_snapshot == "earning" and not line.display_only:
                    account = component.expense_account if component and component.expense_account_id else payroll_expense
                    account = validate_account(account, "expense", "مصروف المرتبات")
                    add_line(account, line.amount, 0, line.name_snapshot, result.employee.cost_center)
                elif line.kind_snapshot == "employer_share" and line.display_only:
                    if component and component.method == component.Method.EMPLOYER_INSURANCE:
                        expense = component.expense_account or get_account_for_role(cycle.company, "employer_insurance_expense")
                        payable = component.payable_account or get_account_for_role(cycle.company, "employer_insurance_payable")
                    else:
                        expense, payable = (component.expense_account, component.payable_account) if component else (None, None)
                    expense = validate_account(expense, "expense", "حصة صاحب العمل")
                    payable = validate_account(payable, "liability", "التزام حصة صاحب العمل")
                    add_line(expense, line.amount, 0, line.name_snapshot, result.employee.cost_center)
                    add_line(payable, 0, line.amount, line.name_snapshot, result.employee.cost_center)
                elif line.kind_snapshot == "deduction" and not line.display_only:
                    if line.code_snapshot.startswith("LOAN-"):
                        installment_id = line.calculation_snapshot.get("installment_id")
                        installment = LoanInstallment.objects.select_related("loan", "loan__receivable_account").get(pk=installment_id)
                        account = validate_account(installment.loan.receivable_account, "asset", "ذمم سلف الموظفين")
                    elif component and component.method == component.Method.EMPLOYEE_INSURANCE:
                        account = component.payable_account or get_account_for_role(cycle.company, "employee_insurance_payable")
                        account = validate_account(account, "liability", "تأمينات الموظفين")
                    elif component and component.method == component.Method.PROGRESSIVE_TAX:
                        account = component.payable_account or get_account_for_role(cycle.company, "payroll_tax_payable")
                        account = validate_account(account, "liability", "ضريبة المرتبات")
                    else:
                        account = validate_account(component.payable_account if component else None, "liability", line.name_snapshot)
                    add_line(account, 0, line.amount, line.name_snapshot, result.employee.cost_center)
            add_line(payroll_payable, 0, result.net_pay, f"صافي مرتب {result.employee.full_name_ar}", result.employee.cost_center)

        if line_count < 2:
            raise ValidationError(f"لا توجد بنود محاسبية كافية لترحيل فرع {branch.name_ar}.")
        totals = JournalLine.objects.filter(entry=entry).aggregate(debit=Sum("debit"), credit=Sum("credit"))
        debit_total = money(totals["debit"] or Decimal("0"))
        credit_total = money(totals["credit"] or Decimal("0"))
        if debit_total != credit_total:
            raise ValidationError(
                f"قيد مرتبات فرع {branch.name_ar} غير متوازن: مدين {debit_total} ودائن {credit_total}."
            )
        posted_entry = post_journal(entry.pk, actor=actor)
        PayrollPosting.objects.create(cycle=cycle, branch=branch, journal_entry=posted_entry)
        posted.append({"branch_id": branch.pk, "branch_name": branch.name_ar, "journal_entry_id": posted_entry.pk, "entry_number": posted_entry.entry_number})

    cycle.journal_entry = JournalEntry.objects.get(pk=posted[0]["journal_entry_id"])
    cycle.status = PayrollCycle.Status.POSTED
    cycle.save(update_fields=("journal_entry", "status", "updated_at"))
    return {"cycle_id": cycle.pk, "status": cycle.status, "postings": posted}

@transaction.atomic
def prepare_payroll_payment(cycle_id, treasury_account_id, actor=None):
    """إعداد دفعة صرف المرتبات وربطها بحساب الخزينة المحدد."""
    cycle = PayrollCycle.objects.select_for_update().select_related("company").get(pk=cycle_id)
    if cycle.status != PayrollCycle.Status.POSTED:
        raise ValidationError("يجب ترحيل استحقاق المرتبات محاسبيًا قبل إعداد الصرف.")
    postings = list(cycle.postings.select_related("branch").order_by("branch__code"))
    if not postings or any(item.payment_treasury_transaction_id for item in postings):
        raise ValidationError("لا توجد قيود مرتبات جاهزة للصرف أو تم إعداد الصرف من قبل.")
    try:
        treasury_account = TreasuryAccount.objects.select_related("account", "branch").select_for_update(of=("self",)).get(pk=treasury_account_id)
    except (TreasuryAccount.DoesNotExist, TypeError, ValueError):
        raise ValidationError("حساب الخزينة المحدد غير موجود أو أن رقمه غير صالح.") from None
    if treasury_account.company_id != cycle.company_id or not treasury_account.is_active:
        raise ValidationError("حساب الخزينة غير نشط أو لا يتبع الشركة.")
    validate_account = treasury_account.account
    if not validate_account.is_active or validate_account.is_group or not validate_account.allow_posting or validate_account.nature != "asset":
        raise ValidationError("الحساب المحاسبي المرتبط بالخزينة غير صالح.")
    fiscal_year = FiscalYear.objects.filter(
        company=cycle.company, start_date__lte=cycle.payment_date, end_date__gte=cycle.payment_date,
    ).first()
    if not fiscal_year or fiscal_year.status != FiscalYear.Status.OPEN:
        raise ValidationError("لا توجد سنة مالية مفتوحة تغطي تاريخ صرف المرتبات.")
    payroll_payable = get_account_for_role(cycle.company, "payroll_payable")
    prepared = []
    for posting in postings:
        if treasury_account.branch_id and treasury_account.branch_id != posting.branch_id:
            raise ValidationError(f"حساب الخزينة المحدد لا يخدم فرع {posting.branch.name_ar}.")
        results = cycle.results.filter(employee__branch_id=posting.branch_id).select_related("employee__currency")
        currencies = set(results.values_list("employee__currency__code", flat=True).distinct())
        if currencies and currencies != {treasury_account.currency_code}:
            raise ValidationError("عملة حساب الخزينة لا تطابق عملة مرتبات جميع موظفي الفرع؛ لا يوجد تحويل عملات تلقائي في هذه المرحلة.")
        amount = sum((row.net_pay for row in results), Decimal("0"))
        if amount <= 0:
            raise ValidationError(f"صافي الصرف لفرع {posting.branch.name_ar} غير موجب.")
        document_number = f"PAYDISB{cycle.pk}-{posting.branch_id}"
        if TreasuryTransaction.objects.filter(company=cycle.company, document_number=document_number).exists():
            suffix = 2
            while TreasuryTransaction.objects.filter(
                company=cycle.company, document_number=f"{document_number}-{suffix}"
            ).exists():
                suffix += 1
            document_number = f"{document_number}-{suffix}"
        document = TreasuryTransaction.objects.create(
            company=cycle.company,
            branch=posting.branch,
            fiscal_year=fiscal_year,
            source_account=treasury_account,
            counter_account=payroll_payable,
            kind=TreasuryTransaction.Kind.PAYMENT,
            document_number=document_number,
            transaction_date=cycle.payment_date,
            amount=money(amount),
            description=f"صرف مرتبات دورة {cycle.pk} — {posting.branch.name_ar}",
            cash_flow_category=TreasuryTransaction.CashFlowCategory.OPERATING,
            created_by=actor,
        )
        approval = refresh_financial_approval(
            FinancialApprovalRequest.DocumentType.TREASURY_PAYMENT, document, actor
        )
        posting.payment_treasury_transaction = document
        posting.save(update_fields=("payment_treasury_transaction",))
        prepared.append({
            "branch_id": posting.branch_id,
            "branch_name": posting.branch.name_ar,
            "treasury_transaction_id": document.pk,
            "amount": str(document.amount),
            "approval_required": bool(approval),
        })
    cycle.status = PayrollCycle.Status.PAYMENT_PENDING
    cycle.save(update_fields=("status", "updated_at"))
    return {"cycle_id": cycle.pk, "status": cycle.status, "payments": prepared}


@transaction.atomic
def complete_payroll_payment(cycle_id, actor=None):
    """إتمام صرف دفعة المرتبات وتسجيل أثر السداد."""
    cycle = PayrollCycle.objects.select_for_update().select_related("company").get(pk=cycle_id)
    if cycle.status != PayrollCycle.Status.PAYMENT_PENDING:
        raise ValidationError("يجب إعداد مستندات الصرف أولًا.")
    postings = list(cycle.postings.select_related("branch", "payment_treasury_transaction").order_by("branch__code"))
    if not postings or any(not item.payment_treasury_transaction_id for item in postings):
        raise ValidationError("مستندات الصرف غير مكتملة لكل الفروع.")
    completed = []
    payroll_payable = get_account_for_role(cycle.company, "payroll_payable")
    for posting in postings:
        document = TreasuryTransaction.objects.select_for_update(of=("self",)).select_related(
            "source_account", "counter_account", "branch", "fiscal_year"
        ).get(pk=posting.payment_treasury_transaction_id)
        branch_results = cycle.results.filter(employee__branch_id=posting.branch_id)
        expected_amount = money(sum((row.net_pay for row in branch_results), Decimal("0")))
        if (
            document.company_id != cycle.company_id
            or document.branch_id != posting.branch_id
            or document.kind != TreasuryTransaction.Kind.PAYMENT
            or document.status != TreasuryTransaction.Status.DRAFT
            or document.amount != expected_amount
            or document.counter_account_id != payroll_payable.pk
            or not document.source_account.is_active
            or document.source_account.company_id != cycle.company_id
            or (document.source_account.branch_id and document.source_account.branch_id != posting.branch_id)
            or document.source_account.currency_code not in set(
                branch_results.values_list("employee__currency__code", flat=True).distinct()
            )
        ):
            raise ValidationError("بيانات مستند صرف المرتبات لا تطابق صافي استحقاق الفرع أو حساب الخزينة المعتمد. لم يتم إلغاء الإعداد تلقائيًا؛ أوقف العملية وراجع المستندات المسودة وطلبات الموافقة قبل إعادة المحاولة.")
        document = post_treasury_transaction(document.pk, actor)
        posting.payment_journal_entry = document.journal_entry
        posting.save(update_fields=("payment_journal_entry",))
        branch_results = cycle.results.filter(employee__branch_id=posting.branch_id).prefetch_related("lines")
        for result in branch_results:
            for line in result.lines.filter(code_snapshot__startswith="LOAN-", kind_snapshot="deduction", display_only=False):
                installment_id = line.calculation_snapshot.get("installment_id")
                installment = LoanInstallment.objects.select_for_update().select_related("loan").get(pk=installment_id)
                remaining = money(installment.amount - installment.paid_amount)
                if line.amount > remaining:
                    raise ValidationError("قيمة قسط السلفة المرحّل تتجاوز الرصيد المستحق.")
                installment.paid_amount = money(installment.paid_amount + line.amount)
                installment.status = LoanInstallment.Status.PAID if installment.paid_amount >= installment.amount else LoanInstallment.Status.PARTIAL
                installment.paid_date = cycle.payment_date if installment.status == LoanInstallment.Status.PAID else None
                installment.payroll_cycle = cycle
                installment.save(update_fields=("paid_amount", "paid_date", "status", "payroll_cycle"))
                loan = installment.loan
                loan.outstanding_balance = max(Decimal("0"), money(loan.outstanding_balance - line.amount))
                if loan.outstanding_balance == 0:
                    loan.status = loan.Status.SETTLED
                loan.save(update_fields=("outstanding_balance", "status"))
        completed.append({
            "branch_id": posting.branch_id,
            "branch_name": posting.branch.name_ar,
            "treasury_transaction_id": document.pk,
            "journal_entry_id": document.journal_entry_id,
        })
    cycle.status = PayrollCycle.Status.PAID
    cycle.save(update_fields=("status", "updated_at"))
    return {"cycle_id": cycle.pk, "status": cycle.status, "payments": completed}



@transaction.atomic
def cancel_payroll_payment_preparation(cycle_id, actor=None):
    """إلغاء إعداد صرف المرتبات دون المساس بقيد الاستحقاق أو أي مستند مرحّل."""
    cycle = PayrollCycle.objects.select_for_update().select_related("company").get(pk=cycle_id)
    if cycle.status != PayrollCycle.Status.PAYMENT_PENDING:
        raise ValidationError("لا توجد عملية إعداد صرف معلقة لإلغائها.")
    postings = list(cycle.postings.select_related("branch", "payment_treasury_transaction").order_by("branch__code"))
    if not postings or any(not item.payment_treasury_transaction_id for item in postings):
        raise ValidationError("مستندات إعداد الصرف غير مكتملة؛ لا يمكن إلغاء العملية بأمان.")
    documents = []
    for posting in postings:
        document = TreasuryTransaction.objects.select_for_update().get(pk=posting.payment_treasury_transaction_id)
        if (
            document.company_id != cycle.company_id
            or document.branch_id != posting.branch_id
            or document.kind != TreasuryTransaction.Kind.PAYMENT
            or document.status != TreasuryTransaction.Status.DRAFT
            or document.journal_entry_id is not None
        ):
            raise ValidationError("توجد حركة صرف غير مسودة أو مرتبطة بقيد؛ أوقف الإلغاء وراجع المستندات يدويًا.")
        documents.append((posting, document))

    cancelled = []
    for posting, document in documents:
        approvals = FinancialApprovalRequest.objects.select_for_update().filter(
            treasury_transaction=document,
            status=FinancialApprovalRequest.Status.PENDING,
        )
        for approval in approvals:
            approval.status = FinancialApprovalRequest.Status.SUPERSEDED
            approval.save(update_fields=("status", "decided_at"))
        # The treasury model intentionally freezes reference and financial fields
        # once linked to a payroll posting. VOID is the only allowed lifecycle
        # transition here; the payroll posting is detached immediately below.
        document.status = TreasuryTransaction.Status.VOID
        document.save(update_fields=("status",))
        posting.payment_treasury_transaction = None
        posting.payment_journal_entry = None
        posting.save(update_fields=("payment_treasury_transaction", "payment_journal_entry"))
        cancelled.append({"branch_id": posting.branch_id, "treasury_transaction_id": document.pk})

    cycle.status = PayrollCycle.Status.POSTED
    cycle.save(update_fields=("status", "updated_at"))
    return {"cycle_id": cycle.pk, "status": cycle.status, "cancelled_documents": cancelled}

def models_q_end(start_date):
    """إنشاء شرط استعلام يضم السجلات التي يبدأ تاريخها قبل نهاية الفترة."""
    from django.db.models import Q
    return Q(effective_to__isnull=True) | Q(effective_to__gte=start_date)
