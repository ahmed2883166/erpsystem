from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from companies.models import Branch, Company


class CompanyScopedModel(models.Model):
    company = models.ForeignKey(Company, on_delete=models.PROTECT)

    class Meta:
        abstract = True


class Department(CompanyScopedModel):
    code = models.CharField(max_length=30)
    name_ar = models.CharField(max_length=160)
    branch = models.ForeignKey(Branch, null=True, blank=True, on_delete=models.PROTECT)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("code",)
        constraints = [models.UniqueConstraint(fields=("company", "code"), name="uq_hr_department_code")]

    def clean(self):
        super().clean()
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "الفرع يجب أن يتبع نفس الشركة."})


class JobPosition(CompanyScopedModel):
    code = models.CharField(max_length=30)
    name_ar = models.CharField(max_length=160)
    department = models.ForeignKey(Department, null=True, blank=True, on_delete=models.PROTECT)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("code",)
        constraints = [models.UniqueConstraint(fields=("company", "code"), name="uq_hr_position_code")]

    def clean(self):
        super().clean()
        if self.department_id and self.department.company_id != self.company_id:
            raise ValidationError({"department": "الإدارة يجب أن تتبع نفس الشركة."})


class Employee(CompanyScopedModel):
    class Status(models.TextChoices):
        ACTIVE = "active", "نشط"
        LEAVE = "leave", "إجازة"
        UNPAID_LEAVE = "unpaid_leave", "إجازة بدون راتب"
        SUSPENDED = "suspended", "موقوف"
        RESIGNED = "resigned", "مستقيل"
        RETIRED = "retired", "متقاعد"

    class ContractType(models.TextChoices):
        PERMANENT = "permanent", "دائم"
        FIXED = "fixed", "محدد المدة"
        TEMPORARY = "temporary", "مؤقت"
        PART_TIME = "part_time", "دوام جزئي"

    code = models.CharField(max_length=30)
    full_name_ar = models.CharField(max_length=180)
    full_name_en = models.CharField(max_length=180, blank=True)
    national_id = models.CharField(max_length=40, blank=True)
    insurance_number = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    address = models.TextField(blank=True)
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="employees")
    department = models.ForeignKey(Department, null=True, blank=True, on_delete=models.PROTECT)
    position = models.ForeignKey(JobPosition, null=True, blank=True, on_delete=models.PROTECT)
    manager = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="direct_reports")
    contract_type = models.CharField(max_length=16, choices=ContractType.choices, default=ContractType.PERMANENT)
    hire_date = models.DateField()
    contract_end_date = models.DateField(null=True, blank=True)
    termination_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    basic_salary = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    currency = models.ForeignKey("companies.Currency", on_delete=models.PROTECT)
    payment_method = models.CharField(max_length=12, choices=(("bank", "تحويل بنكي"), ("cash", "نقدي")), default="bank")
    bank_name = models.CharField(max_length=120, blank=True)
    bank_account = models.CharField(max_length=80, blank=True)
    cost_center = models.ForeignKey("accounting.CostCenter", null=True, blank=True, on_delete=models.PROTECT)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="hr_employees_created")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("code",)
        constraints = [models.UniqueConstraint(fields=("company", "code"), name="uq_hr_employee_code")]
        indexes = [
            models.Index(fields=("company", "status", "branch"), name="hr_employee_scope_idx"),
            models.Index(fields=("company", "department", "status"), name="hr_employee_dept_idx"),
        ]

    def clean(self):
        super().clean()
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "الفرع يجب أن يتبع نفس الشركة."})
        for field in ("department", "position", "manager", "cost_center"):
            value = getattr(self, field, None)
            if value and value.company_id != self.company_id:
                raise ValidationError({field: "يجب أن يتبع نفس الشركة."})
        if self.basic_salary < 0:
            raise ValidationError({"basic_salary": "الراتب الأساسي لا يمكن أن يكون سالبًا."})
        if self.termination_date and self.termination_date < self.hire_date:
            raise ValidationError({"termination_date": "تاريخ انتهاء الخدمة يسبق تاريخ التعيين."})


class PayrollComponent(CompanyScopedModel):
    class Kind(models.TextChoices):
        EARNING = "earning", "استحقاق"
        DEDUCTION = "deduction", "استقطاع موظف"
        EMPLOYER_SHARE = "employer_share", "حصة صاحب العمل"

    class Method(models.TextChoices):
        FIXED = "fixed", "مبلغ ثابت"
        PERCENT_BASIC = "percent_basic", "نسبة من الأساسي"
        PER_DAY = "per_day", "حسب الأيام"
        PER_HOUR = "per_hour", "حسب الساعات"
        MANUAL = "manual", "إدخال يدوي"

    code = models.CharField(max_length=32)
    name_ar = models.CharField(max_length=160)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    method = models.CharField(max_length=20, choices=Method.choices, default=Method.FIXED)
    default_value = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    affects_taxable_base = models.BooleanField(default=False)
    affects_insurance_base = models.BooleanField(default=False)
    requires_approval = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    expense_account = models.ForeignKey("accounting.ChartOfAccount", null=True, blank=True, on_delete=models.PROTECT)
    payable_account = models.ForeignKey("accounting.ChartOfAccount", null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        ordering = ("kind", "code")
        constraints = [models.UniqueConstraint(fields=("company", "code"), name="uq_hr_component_code")]

    def clean(self):
        super().clean()
        for field in ("expense_account", "payable_account"):
            account = getattr(self, field, None)
            if account and account.company_id != self.company_id:
                raise ValidationError({field: "الحساب المحاسبي يجب أن يتبع نفس الشركة."})


class EmployeeComponent(models.Model):
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="compensation_items")
    component = models.ForeignKey(PayrollComponent, on_delete=models.PROTECT, related_name="employee_assignments")
    value = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-effective_from",)
        constraints = [models.CheckConstraint(condition=Q(effective_to__isnull=True) | Q(effective_to__gte=models.F("effective_from")), name="ck_hr_component_dates")]

    def clean(self):
        super().clean()
        if self.employee_id and self.component_id and self.employee.company_id != self.component.company_id:
            raise ValidationError("عنصر المرتب والموظف يجب أن يتبعا نفس الشركة.")
        if self.value is not None and self.value < 0:
            raise ValidationError({"value": "قيمة العنصر لا يمكن أن تكون سالبة."})


class AttendanceRecord(models.Model):
    class Status(models.TextChoices):
        PRESENT = "present", "حضور"
        ABSENT = "absent", "غياب"
        LEAVE = "leave", "إجازة"
        UNPAID_LEAVE = "unpaid_leave", "إجازة بدون راتب"
        HOLIDAY = "holiday", "عطلة"

    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="attendance")
    work_date = models.DateField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PRESENT)
    worked_hours = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    overtime_hours = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    late_minutes = models.PositiveIntegerField(default=0)
    notes = models.CharField(max_length=300, blank=True)
    entered_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        ordering = ("work_date", "employee__code")
        constraints = [models.UniqueConstraint(fields=("employee", "work_date"), name="uq_hr_attendance_day")]
        indexes = [models.Index(fields=("employee", "work_date"), name="hr_attendance_period_idx")]

    def clean(self):
        super().clean()
        if self.worked_hours < 0 or self.overtime_hours < 0:
            raise ValidationError("ساعات العمل لا يمكن أن تكون سالبة.")


class LeaveRequest(models.Model):
    class Kind(models.TextChoices):
        ANNUAL = "annual", "سنوية"
        SICK = "sick", "مرضية"
        EMERGENCY = "emergency", "عارضة"
        UNPAID = "unpaid", "بدون راتب"

    class Status(models.TextChoices):
        PENDING = "pending", "قيد المراجعة"
        APPROVED = "approved", "معتمدة"
        REJECTED = "rejected", "مرفوضة"

    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="leave_requests")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    start_date = models.DateField()
    end_date = models.DateField()
    days = models.DecimalField(max_digits=8, decimal_places=2)
    reason = models.TextField(blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="approved_hr_leaves")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-start_date",)
        constraints = [models.CheckConstraint(condition=Q(end_date__gte=models.F("start_date")) & Q(days__gt=0), name="ck_hr_leave_dates_days")]

    def clean(self):
        super().clean()
        if self.end_date < self.start_date or self.days <= 0:
            raise ValidationError("راجع فترة الإجازة وعدد الأيام.")


class EmployeeLoan(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "سارية"
        SETTLED = "settled", "مسددة"
        SUSPENDED = "suspended", "موقوفة"

    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="loans")
    reference = models.CharField(max_length=40)
    issue_date = models.DateField()
    principal = models.DecimalField(max_digits=18, decimal_places=4)
    installment_count = models.PositiveSmallIntegerField()
    installment_amount = models.DecimalField(max_digits=18, decimal_places=4)
    outstanding_balance = models.DecimalField(max_digits=18, decimal_places=4)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    notes = models.TextField(blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("employee", "reference"), name="uq_hr_loan_reference")]

    def clean(self):
        super().clean()
        if self.principal <= 0 or self.installment_count <= 0 or self.installment_amount <= 0:
            raise ValidationError("قيمة السلفة وعدد الأقساط وقيمة القسط يجب أن تكون موجبة.")
        if self.outstanding_balance < 0 or self.outstanding_balance > self.principal:
            raise ValidationError("رصيد السلفة المتبقي غير صحيح.")


class EmployeeSalaryHistory(models.Model):
    class ChangeType(models.TextChoices):
        ANNUAL_RAISE = "annual_raise", "زيادة سنوية"
        PROMOTION = "promotion", "ترقية"
        ADJUSTMENT = "adjustment", "تعديل"

    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="salary_history")
    effective_date = models.DateField()
    previous_salary = models.DecimalField(max_digits=18, decimal_places=4)
    new_salary = models.DecimalField(max_digits=18, decimal_places=4)
    change_type = models.CharField(max_length=16, choices=ChangeType.choices, default=ChangeType.ADJUSTMENT)
    reason = models.TextField(blank=True)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-effective_date", "-id")
        constraints = [models.CheckConstraint(condition=Q(new_salary__gte=0) & Q(previous_salary__gte=0), name="ck_hr_salary_nonnegative")]


class LoanInstallment(models.Model):
    class Status(models.TextChoices):
        DUE = "due", "مستحق"
        PAID = "paid", "مسدد"
        PARTIAL = "partial", "مسدد جزئيًا"
        WAIVED = "waived", "معفى"

    loan = models.ForeignKey(EmployeeLoan, on_delete=models.PROTECT, related_name="installments")
    due_date = models.DateField()
    amount = models.DecimalField(max_digits=18, decimal_places=4)
    paid_amount = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    paid_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DUE)
    payroll_cycle = models.ForeignKey("hr.PayrollCycle", null=True, blank=True, on_delete=models.PROTECT, related_name="loan_installments")

    class Meta:
        ordering = ("due_date", "id")
        constraints = [models.UniqueConstraint(fields=("loan", "due_date"), name="uq_hr_loan_installment_date"), models.CheckConstraint(condition=Q(amount__gt=0) & Q(paid_amount__gte=0) & Q(paid_amount__lte=models.F("amount")), name="ck_hr_loan_installment_amount")]


class PayrollRuleSet(CompanyScopedModel):
    country = models.CharField(max_length=2, choices=(("EG", "مصر"), ("SA", "السعودية")))
    name = models.CharField(max_length=120)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    configuration = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-effective_from",)
        constraints = [models.CheckConstraint(condition=Q(effective_to__isnull=True) | Q(effective_to__gte=models.F("effective_from")), name="ck_hr_ruleset_dates")]

    def clean(self):
        super().clean()
        if self.country != self.company.country:
            raise ValidationError({"country": "بلد مجموعة القواعد يجب أن يطابق بلد الشركة."})


class PayrollCycle(CompanyScopedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        CALCULATED = "calculated", "تم الاحتساب"
        REVIEW = "review", "تحت المراجعة"
        APPROVED = "approved", "معتمدة"
        POSTED = "posted", "مرحّلة"
        PAID = "paid", "تم الصرف"

    period_start = models.DateField()
    period_end = models.DateField()
    payment_date = models.DateField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    rule_set = models.ForeignKey(PayrollRuleSet, null=True, blank=True, on_delete=models.PROTECT)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="created_payroll_cycles")
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="approved_payroll_cycles")
    approved_at = models.DateTimeField(null=True, blank=True)
    journal_entry = models.OneToOneField("accounting.JournalEntry", null=True, blank=True, on_delete=models.PROTECT, related_name="payroll_cycle")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-period_start",)
        constraints = [models.UniqueConstraint(fields=("company", "period_start", "period_end"), name="uq_hr_payroll_period"), models.CheckConstraint(condition=Q(period_end__gte=models.F("period_start")), name="ck_hr_payroll_period_dates")]

    def clean(self):
        super().clean()
        if self.period_end < self.period_start or not (self.period_start <= self.payment_date):
            raise ValidationError("راجع بداية ونهاية الفترة وتاريخ الصرف.")
        if self.rule_set_id and self.rule_set.company_id != self.company_id:
            raise ValidationError({"rule_set": "مجموعة القواعد يجب أن تتبع نفس الشركة."})
        if self.journal_entry_id and self.journal_entry.company_id != self.company_id:
            raise ValidationError({"journal_entry": "قيد المرتبات يجب أن يتبع نفس الشركة."})

    def save(self, *args, **kwargs):
        if self.pk:
            old = type(self).objects.filter(pk=self.pk).first()
            if old and old.status in (self.Status.APPROVED, self.Status.POSTED, self.Status.PAID):
                protected = ("company_id", "period_start", "period_end", "payment_date", "rule_set_id")
                if any(getattr(old, key) != getattr(self, key) for key in protected):
                    raise ValidationError("الدورة المرحلة أو المصروفة لا يمكن تغيير بياناتها الأساسية.")
        return super().save(*args, **kwargs)


class PayrollResult(models.Model):
    cycle = models.ForeignKey(PayrollCycle, on_delete=models.PROTECT, related_name="results")
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="payroll_results")
    basic_salary_snapshot = models.DecimalField(max_digits=18, decimal_places=4)
    earnings_total = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    employee_deductions_total = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    employer_share_total = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    employer_share_display_addition = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    employer_share_display_offset = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    net_pay = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    company_cost = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    calculation_snapshot = models.JSONField(default=dict, blank=True)
    manual_adjustment_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("cycle", "employee"), name="uq_hr_payroll_employee_cycle")]
        ordering = ("employee__code",)

    @property
    def displayed_earnings_total(self):
        return self.earnings_total + self.employer_share_display_addition

    @property
    def displayed_deductions_total(self):
        return self.employee_deductions_total + self.employer_share_display_offset

    def clean(self):
        super().clean()
        if self.cycle_id and self.employee_id and self.cycle.company_id != self.employee.company_id:
            raise ValidationError("الموظف ودورة المرتبات يجب أن يتبعا نفس الشركة.")
        if self.employer_share_display_addition != self.employer_share_total or self.employer_share_display_offset != self.employer_share_total:
            raise ValidationError("يجب إظهار حصة صاحب العمل بالقيمة نفسها في جانبي الكشف.")
        if self.net_pay != self.earnings_total - self.employee_deductions_total:
            raise ValidationError("صافي المرتب يجب ألا يتأثر بعرض حصة صاحب العمل.")


class PayrollResultLine(models.Model):
    result = models.ForeignKey(PayrollResult, on_delete=models.PROTECT, related_name="lines")
    component = models.ForeignKey(PayrollComponent, null=True, blank=True, on_delete=models.PROTECT)
    code_snapshot = models.CharField(max_length=32)
    name_snapshot = models.CharField(max_length=160)
    kind_snapshot = models.CharField(max_length=20)
    amount = models.DecimalField(max_digits=18, decimal_places=4)
    display_only = models.BooleanField(default=False)
    calculation_snapshot = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("id",)
