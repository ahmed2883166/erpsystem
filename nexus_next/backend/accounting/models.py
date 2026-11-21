from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from companies.models import Branch, Company, FiscalYear


class ChartOfAccount(models.Model):
    class Nature(models.TextChoices):
        ASSET = "asset", "أصول"
        LIABILITY = "liability", "التزامات"
        EQUITY = "equity", "حقوق ملكية"
        REVENUE = "revenue", "إيرادات"
        EXPENSE = "expense", "مصروفات"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="accounts")
    code = models.CharField(max_length=40)
    name_ar = models.CharField(max_length=180)
    name_en = models.CharField(max_length=180, blank=True)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children")
    nature = models.CharField(max_length=12, choices=Nature.choices)
    is_group = models.BooleanField(default=False)
    allow_posting = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("code",)
        constraints = [models.UniqueConstraint(fields=("company", "code"), name="uq_account_company_code")]
        indexes = [models.Index(fields=("company", "nature", "is_active"))]
        verbose_name = "حساب"
        verbose_name_plural = "دليل الحسابات"

    def clean(self):
        super().clean()
        if self.parent_id:
            if self.parent.company_id != self.company_id:
                raise ValidationError({"parent": "الحساب الأب يجب أن يتبع نفس الشركة."})
            if self.parent_id == self.pk:
                raise ValidationError({"parent": "لا يمكن أن يكون الحساب أبًا لنفسه."})
            if not self.parent.is_group:
                raise ValidationError({"parent": "يجب اختيار حساب تجميعي كحساب أب."})
        if self.is_group and self.allow_posting:
            raise ValidationError({"allow_posting": "الحساب التجميعي لا يقبل الترحيل المباشر."})

    def __str__(self):
        return f"{self.code} - {self.name_ar}"


class AccountRole(models.Model):
    class Role(models.TextChoices):
        CASH = "cash", "النقدية"
        BANK = "bank", "البنوك"
        ACCOUNTS_RECEIVABLE = "accounts_receivable", "العملاء"
        ACCOUNTS_PAYABLE = "accounts_payable", "الموردون"
        INVENTORY = "inventory", "المخزون"
        SALES = "sales", "المبيعات"
        SALES_RETURNS = "sales_returns", "مردودات المبيعات"
        COGS = "cogs", "تكلفة المبيعات"
        OPERATING_EXPENSE = "operating_expense", "المصروفات التشغيلية"
        VAT_INPUT = "vat_input", "ضريبة القيمة المضافة - مدخلات"
        VAT_OUTPUT = "vat_output", "ضريبة القيمة المضافة - مخرجات"
        RETAINED_EARNINGS = "retained_earnings", "الأرباح المحتجزة"
        PAYROLL_EXPENSE = "payroll_expense", "مصروف المرتبات"
        PAYROLL_PAYABLE = "payroll_payable", "مرتبات مستحقة"
        GOODS_RECEIVED_NOT_INVOICED = "goods_received_not_invoiced", "بضاعة مستلمة غير مفوترة"
        INVENTORY_ADJUSTMENT_GAIN = "inventory_adjustment_gain", "أرباح تسوية المخزون"
        INVENTORY_ADJUSTMENT_LOSS = "inventory_adjustment_loss", "خسائر تسوية المخزون"
        INTER_BRANCH_CLEARING = "inter_branch_clearing", "حساب تسوية بين الفروع"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="account_roles")
    role = models.CharField(max_length=32, choices=Role.choices)
    account = models.ForeignKey(ChartOfAccount, on_delete=models.PROTECT, related_name="role_assignments")

    class Meta:
        constraints = [models.UniqueConstraint(fields=("company", "role"), name="uq_account_role_company")]
        verbose_name = "ربط دور حسابي"
        verbose_name_plural = "أدوار الحسابات"

    def clean(self):
        super().clean()
        if self.account_id and self.account.company_id != self.company_id:
            raise ValidationError({"account": "الحساب المختار لا يتبع نفس الشركة."})
        if self.account_id and (self.account.is_group or not self.account.allow_posting):
            raise ValidationError({"account": "يجب ربط الدور بحساب تفصيلي قابل للترحيل."})


class TaxCode(models.Model):
    class Category(models.TextChoices):
        STANDARD = "standard", "خاضع بالنسبة القياسية"
        ZERO_RATED = "zero_rated", "نسبة صفرية"
        EXEMPT = "exempt", "معفى"
        OUT_OF_SCOPE = "out_of_scope", "خارج النطاق"
        REVERSE_CHARGE = "reverse_charge", "احتساب عكسي"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="tax_codes")
    country = models.CharField(max_length=2, choices=Company.Country.choices)
    code = models.CharField(max_length=32)
    name_ar = models.CharField(max_length=160)
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.STANDARD)
    rate = models.DecimalField(max_digits=7, decimal_places=4, default=0)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    input_tax_recoverable = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("code", "effective_from")
        constraints = [
            models.UniqueConstraint(fields=("company", "code", "effective_from"), name="uq_tax_code_company_start"),
            models.CheckConstraint(condition=Q(rate__gte=0) & Q(rate__lte=100), name="ck_tax_code_rate_range"),
            models.CheckConstraint(condition=Q(effective_to__isnull=True) | Q(effective_to__gte=models.F("effective_from")), name="ck_tax_code_effective_dates"),
        ]
        indexes = [models.Index(fields=("company", "country", "is_active"), name="acct_tax_company_active_idx")]

    def clean(self):
        super().clean()
        if self.company_id and self.country != self.company.country:
            raise ValidationError({"country": "بلد الضريبة يجب أن يطابق بلد الشركة."})
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            raise ValidationError({"effective_to": "تاريخ انتهاء الضريبة يجب ألا يسبق تاريخ بدايتها."})
        if self.category in (self.Category.ZERO_RATED, self.Category.EXEMPT, self.Category.OUT_OF_SCOPE) and self.rate != 0:
            raise ValidationError({"rate": "هذه الفئة يجب أن تكون بنسبة صفر."})
        if self.category == self.Category.STANDARD and self.rate <= 0:
            raise ValidationError({"rate": "أدخل نسبة موجبة للفئة القياسية."})

    def save(self, *args, **kwargs):
        if self.pk:
            old = type(self).objects.filter(pk=self.pk).first()
            if old and (old.sales_invoice_lines.exists() or old.purchase_invoice_lines.exists()):
                protected = (
                    "company_id", "country", "code", "name_ar", "category", "rate",
                    "effective_from", "effective_to", "input_tax_recoverable",
                )
                if any(getattr(old, field) != getattr(self, field) for field in protected):
                    raise ValidationError("كود الضريبة مستخدم في فواتير؛ لا يمكن تغيير بياناته التاريخية. أنشئ إصدارًا جديدًا.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.sales_invoice_lines.exists() or self.purchase_invoice_lines.exists():
            raise ValidationError("لا يمكن حذف كود ضريبي مرتبط بفواتير؛ عطّله فقط.")
        return super().delete(*args, **kwargs)


class CostCenter(models.Model):
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="cost_centers")
    code = models.CharField(max_length=32)
    name_ar = models.CharField(max_length=160)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("code",)
        constraints = [models.UniqueConstraint(fields=("company", "code"), name="uq_cost_center_company_code")]

    def clean(self):
        super().clean()
        if self.parent_id and self.parent.company_id != self.company_id:
            raise ValidationError({"parent": "مركز التكلفة الأب يجب أن يتبع نفس الشركة."})


class JournalEntry(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        POSTED = "posted", "مرحّل"
        REVERSED = "reversed", "معكوس"

    class Source(models.TextChoices):
        MANUAL = "manual", "يدوي"
        SALES = "sales", "مبيعات"
        PURCHASES = "purchases", "مشتريات"
        PAYROLL = "payroll", "مرتبات"
        INVENTORY = "inventory", "مخزون"
        ASSETS = "assets", "أصول ثابتة"
        OPENING = "opening", "افتتاحي"
        CLOSING = "closing", "إقفال"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="journal_entries")
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="journal_entries")
    fiscal_year = models.ForeignKey(FiscalYear, on_delete=models.PROTECT, related_name="journal_entries")
    entry_number = models.CharField(max_length=40)
    entry_date = models.DateField()
    description = models.CharField(max_length=500)
    source_type = models.CharField(max_length=16, choices=Source.choices, default=Source.MANUAL)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    reversal_of = models.OneToOneField("self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversal_entry")
    created_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="created_journals")
    created_at = models.DateTimeField(auto_now_add=True)
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-entry_date", "-entry_number")
        constraints = [models.UniqueConstraint(fields=("company", "fiscal_year", "entry_number"), name="uq_journal_number_year")]
        indexes = [models.Index(fields=("company", "entry_date", "status"))]
        verbose_name = "قيد يومية"
        verbose_name_plural = "قيود اليومية"

    def clean(self):
        super().clean()
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "الفرع يجب أن يتبع نفس الشركة."})
        if self.fiscal_year_id and self.fiscal_year.company_id != self.company_id:
            raise ValidationError({"fiscal_year": "السنة المالية يجب أن تتبع نفس الشركة."})
        if self.reversal_of_id:
            if self.reversal_of.company_id != self.company_id:
                raise ValidationError({"reversal_of": "القيد الأصلي يجب أن يتبع نفس الشركة."})
            if self.reversal_of.reversal_of_id:
                raise ValidationError({"reversal_of": "لا يمكن عكس قيد عكسي."})
            if self.reversal_of.status != self.Status.POSTED:
                raise ValidationError({"reversal_of": "يمكن عكس قيد مرحّل فقط."})
        if self.status != self.Status.DRAFT and self.pk:
            old = type(self).objects.filter(pk=self.pk).values_list("status", flat=True).first()
            if old in (self.Status.POSTED, self.Status.REVERSED):
                raise ValidationError("القيد المرحّل غير قابل للتعديل.")

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values_list("status", flat=True).first()
            if previous in (self.Status.POSTED, self.Status.REVERSED):
                raise ValidationError("القيد المرحّل أو المعكوس غير قابل للتعديل.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status != self.Status.DRAFT:
            raise ValidationError("لا يمكن حذف قيد مرحّل أو معكوس.")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.entry_number} - {self.entry_date}"


class JournalLine(models.Model):
    entry = models.ForeignKey(JournalEntry, on_delete=models.PROTECT, related_name="lines")
    account = models.ForeignKey(ChartOfAccount, on_delete=models.PROTECT, related_name="journal_lines")
    description = models.CharField(max_length=300, blank=True)
    debit = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    credit = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    cost_center = models.ForeignKey(CostCenter, null=True, blank=True, on_delete=models.PROTECT, related_name="journal_lines")

    class Meta:
        ordering = ("id",)
        constraints = [
            models.CheckConstraint(condition=Q(debit__gte=0) & Q(credit__gte=0), name="ck_journal_line_nonnegative"),
            models.CheckConstraint(condition=(Q(debit__gt=0, credit=0) | Q(credit__gt=0, debit=0)), name="ck_journal_line_one_side"),
        ]

    def save(self, *args, **kwargs):
        if self.entry_id and self.entry.status != JournalEntry.Status.DRAFT:
            raise ValidationError("لا يمكن تعديل سطور قيد مرحّل أو معكوس.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.entry.status != JournalEntry.Status.DRAFT:
            raise ValidationError("لا يمكن حذف سطور قيد مرحّل أو معكوس.")
        return super().delete(*args, **kwargs)

    def clean(self):
        super().clean()
        if self.entry_id and self.account_id and self.entry.company_id != self.account.company_id:
            raise ValidationError({"account": "الحساب يجب أن يتبع نفس شركة القيد."})
        if self.cost_center_id and self.entry_id and self.cost_center.company_id != self.entry.company_id:
            raise ValidationError({"cost_center": "مركز التكلفة يجب أن يتبع نفس الشركة."})
