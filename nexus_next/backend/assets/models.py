"""نماذج الأصول الثابتة وأقساط الإهلاك المرتبطة بها."""

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from companies.models import Company, Branch
from accounting.models import ChartOfAccount, JournalEntry


class FixedAsset(models.Model):
    """يحفظ بيانات الأصل وتكلفته وعمره وحالته المحاسبية."""
    class Status(models.TextChoices):
        ACTIVE = "active", "نشط"
        DISPOSED = "disposed", "مستبعد"

    class Method(models.TextChoices):
        STRAIGHT_LINE = "straight_line", "القسط الثابت"
        DECLINING_BALANCE = "declining_balance", "القسط المتناقص"
        UNITS_OF_PRODUCTION = "units_of_production", "وحدات الإنتاج"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="fixed_assets")
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="fixed_assets")
    code = models.CharField(max_length=40)
    name_ar = models.CharField(max_length=180)
    class Category(models.TextChoices):
        LAND = "land", "أراضٍ"
        BUILDING = "building", "مبانٍ"
        VEHICLE = "vehicle", "سيارات ووسائل نقل"
        FURNITURE = "furniture", "أثاث وتجهيزات"
        EQUIPMENT = "equipment", "معدات وآلات"
        COMPUTERS = "computers", "أجهزة حاسب وتقنية"
        OTHER = "other", "أخرى"

    category = models.CharField(max_length=20, choices=Category.choices, default=Category.OTHER)
    description = models.TextField(blank=True)
    location = models.CharField(max_length=180, blank=True)
    custodian = models.CharField(max_length=180, blank=True)
    custodian_employee = models.ForeignKey("hr.Employee", null=True, blank=True, on_delete=models.PROTECT, related_name="fixed_assets_custodied")
    serial_number = models.CharField(max_length=100, blank=True)
    warranty_end_date = models.DateField(null=True, blank=True)
    supplier_name = models.CharField(max_length=180, blank=True)
    supplier_invoice = models.CharField(max_length=100, blank=True)
    acquisition_date = models.DateField()
    depreciation_start_date = models.DateField()
    original_cost = models.DecimalField(max_digits=20, decimal_places=4)
    acquisition_net_amount = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    recoverable_tax_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    nonrecoverable_tax_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    acquisition_tax_code = models.ForeignKey(
        "accounting.TaxCode", null=True, blank=True, on_delete=models.PROTECT,
        related_name="fixed_asset_acquisitions",
    )
    salvage_value = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    useful_life_months = models.PositiveIntegerField()
    depreciation_method = models.CharField(max_length=24, choices=Method.choices, default=Method.STRAIGHT_LINE)
    declining_rate_percentage = models.DecimalField(max_digits=7, decimal_places=4, null=True, blank=True)
    production_units_total = models.PositiveBigIntegerField(null=True, blank=True)
    asset_account = models.ForeignKey(ChartOfAccount, on_delete=models.PROTECT, related_name="fixed_asset_costs")
    accumulated_depreciation_account = models.ForeignKey(ChartOfAccount, on_delete=models.PROTECT, related_name="fixed_asset_accumulated")
    depreciation_expense_account = models.ForeignKey(ChartOfAccount, on_delete=models.PROTECT, related_name="fixed_asset_expenses")
    acquisition_counter_account = models.ForeignKey(ChartOfAccount, on_delete=models.PROTECT, related_name="fixed_asset_acquisitions")
    disposal_gain_loss_account = models.ForeignKey(ChartOfAccount, on_delete=models.PROTECT, related_name="fixed_asset_disposals")
    disposal_loss_account = models.ForeignKey(ChartOfAccount, null=True, blank=True, on_delete=models.PROTECT, related_name="fixed_asset_disposal_losses")
    acquisition_journal = models.OneToOneField(JournalEntry, null=True, blank=True, on_delete=models.PROTECT, related_name="acquired_fixed_asset")
    acquisition_treasury_transaction = models.OneToOneField("treasury.TreasuryTransaction", null=True, blank=True, on_delete=models.PROTECT, related_name="fixed_asset_acquisition")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    disposed_at = models.DateField(null=True, blank=True)
    disposal_proceeds = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    disposal_journal = models.OneToOneField(JournalEntry, null=True, blank=True, on_delete=models.PROTECT, related_name="disposed_fixed_asset")
    disposal_treasury_transaction = models.OneToOneField("treasury.TreasuryTransaction", null=True, blank=True, on_delete=models.PROTECT, related_name="fixed_asset_disposal")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("code",)
        constraints = [
            models.UniqueConstraint(fields=("company", "code"), name="uq_asset_company_code"),
            models.CheckConstraint(condition=Q(original_cost__gt=0), name="ck_asset_cost_positive"),
            models.CheckConstraint(condition=Q(salvage_value__gte=0), name="ck_asset_salvage_nonnegative"),
            models.CheckConstraint(condition=Q(useful_life_months__gt=0), name="ck_asset_life_positive"),
        ]

    def clean(self):
        super().clean()
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "الفرع لا يتبع الشركة."})
        if self.custodian_employee_id and (self.custodian_employee.company_id != self.company_id or self.custodian_employee.branch_id != self.branch_id):
            raise ValidationError({"custodian_employee": "الموظف المسؤول يجب أن يتبع نفس الشركة والفرع."})
        if self.salvage_value > self.original_cost:
            raise ValidationError({"salvage_value": "القيمة التخريدية لا يمكن أن تتجاوز التكلفة."})
        if self.nonrecoverable_tax_amount < 0 or self.recoverable_tax_amount < 0:
            raise ValidationError({"nonrecoverable_tax_amount": "قيم الضريبة لا يمكن أن تكون سالبة."})
        if self.acquisition_net_amount is not None:
            if self.acquisition_net_amount < 0 or self.acquisition_net_amount + self.nonrecoverable_tax_amount != self.original_cost:
                raise ValidationError({"original_cost": "تكلفة الأصل يجب أن تساوي صافي الاقتناء مضافًا إليه الضريبة غير القابلة للاسترداد."})
        elif self.nonrecoverable_tax_amount or self.recoverable_tax_amount:
            raise ValidationError({"acquisition_net_amount": "أدخل صافي تكلفة الاقتناء لتوثيق ضريبة الاقتناء."})
        if bool(self.acquisition_tax_code_id) != bool(self.recoverable_tax_amount or self.nonrecoverable_tax_amount):
            raise ValidationError({"acquisition_tax_code": "كود الضريبة ومبالغ الضريبة يجب إدخالها معًا."})
        if self.acquisition_tax_code_id:
            tax_code = self.acquisition_tax_code
            if tax_code.company_id != self.company_id:
                raise ValidationError({"acquisition_tax_code": "كود الضريبة لا يتبع الشركة."})
            if tax_code.input_tax_recoverable and self.nonrecoverable_tax_amount:
                raise ValidationError({"nonrecoverable_tax_amount": "هذه الضريبة قابلة للاسترداد ولا تدخل في تكلفة الأصل."})
            if not tax_code.input_tax_recoverable and self.recoverable_tax_amount:
                raise ValidationError({"recoverable_tax_amount": "هذه الضريبة غير قابلة للاسترداد."})
        for field in ("asset_account", "accumulated_depreciation_account", "depreciation_expense_account", "acquisition_counter_account", "disposal_gain_loss_account", "disposal_loss_account"):
            account = getattr(self, field, None)
            if account and account.company_id != self.company_id:
                raise ValidationError({field: "الحساب يجب أن يتبع نفس الشركة."})
        if self.depreciation_start_date < self.acquisition_date:
            raise ValidationError({"depreciation_start_date": "بداية الإهلاك لا تسبق تاريخ الاقتناء."})

    @property
    def accumulated_depreciation(self):
        return self.depreciations.filter(journal_entry__status=JournalEntry.Status.POSTED).aggregate(total=models.Sum("amount"))["total"] or 0

    @property
    def accumulated_impairment(self):
        losses = self.impairments.filter(journal_entry__status=JournalEntry.Status.POSTED).aggregate(total=models.Sum("impairment_loss"))["total"] or 0
        reversals = self.impairment_reversals.filter(journal_entry__status=JournalEntry.Status.POSTED).aggregate(total=models.Sum("reversal_amount"))["total"] or 0
        return losses - reversals

    @property
    def net_book_value(self):
        return self.original_cost - self.accumulated_depreciation - self.accumulated_impairment

    @property
    def depreciable_amount(self):
        return self.original_cost - self.salvage_value

    def __str__(self):
        return f"{self.code} - {self.name_ar}"


class AssetDepreciation(models.Model):
    """يمثل قسط إهلاك الأصل وتاريخ استحقاقه وقيده."""
    asset = models.ForeignKey(FixedAsset, on_delete=models.PROTECT, related_name="depreciations")
    period_date = models.DateField()
    amount = models.DecimalField(max_digits=20, decimal_places=4)
    is_partial_period = models.BooleanField(default=False)
    units_produced = models.PositiveBigIntegerField(null=True, blank=True)
    journal_entry = models.OneToOneField(JournalEntry, on_delete=models.PROTECT, related_name="asset_depreciation")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("period_date",)
        constraints = [models.UniqueConstraint(fields=("asset", "period_date"), condition=Q(is_partial_period=False), name="uq_asset_depr_period")]


class AssetImpairment(models.Model):
    """سجل اختبار انخفاض قيمة الأصل وفق IAS 36 مع قيد مستقل قابل للمطابقة."""
    asset = models.ForeignKey(FixedAsset, on_delete=models.PROTECT, related_name="impairments")
    impairment_date = models.DateField()
    carrying_amount_before = models.DecimalField(max_digits=20, decimal_places=4)
    recoverable_amount = models.DecimalField(max_digits=20, decimal_places=4)
    impairment_loss = models.DecimalField(max_digits=20, decimal_places=4)
    impairment_expense_account = models.ForeignKey(
        ChartOfAccount, on_delete=models.PROTECT, related_name="fixed_asset_impairment_expenses",
    )
    accumulated_impairment_account = models.ForeignKey(
        ChartOfAccount, on_delete=models.PROTECT, related_name="fixed_asset_accumulated_impairments",
    )
    journal_entry = models.OneToOneField(
        JournalEntry, on_delete=models.PROTECT, related_name="asset_impairment",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("impairment_date", "id")
        constraints = [
            models.UniqueConstraint(fields=("asset", "impairment_date"), name="uq_asset_impairment_date"),
            models.CheckConstraint(condition=Q(carrying_amount_before__gt=0), name="ck_asset_impairment_carrying_positive"),
            models.CheckConstraint(condition=Q(recoverable_amount__gte=0), name="ck_asset_impairment_recoverable_nonnegative"),
            models.CheckConstraint(condition=Q(impairment_loss__gt=0), name="ck_asset_impairment_loss_positive"),
        ]

    def clean(self):
        super().clean()
        if self.impairment_loss != self.carrying_amount_before - self.recoverable_amount:
            raise ValidationError({"impairment_loss": "خسارة الانخفاض يجب أن تساوي القيمة الدفترية قبل الانخفاض ناقص القيمة القابلة للاسترداد."})
        if self.impairment_expense_account_id and (
            self.impairment_expense_account.company_id != self.asset.company_id
            or self.impairment_expense_account.nature != "expense"
            or self.impairment_expense_account.is_group
            or not self.impairment_expense_account.allow_posting
        ):
            raise ValidationError({"impairment_expense_account": "اختر حساب مصروف انخفاض قيمة تفصيليًا من نفس الشركة."})
        if self.accumulated_impairment_account_id and (
            self.accumulated_impairment_account.company_id != self.asset.company_id
            or self.accumulated_impairment_account.nature != "asset"
            or self.accumulated_impairment_account.is_group
            or not self.accumulated_impairment_account.allow_posting
        ):
            raise ValidationError({"accumulated_impairment_account": "اختر حسابًا مقابلًا للأصل من نفس الشركة."})
        if self.impairment_expense_account_id == self.accumulated_impairment_account_id:
            raise ValidationError("يجب فصل حساب مصروف الانخفاض عن حساب مجمع الانخفاض.")


class AssetImpairmentReversal(models.Model):
    """IAS 36 reversal, capped at the no-impairment carrying amount under the cost model."""
    asset = models.ForeignKey(FixedAsset, on_delete=models.PROTECT, related_name="impairment_reversals")
    reversal_date = models.DateField()
    carrying_amount_before = models.DecimalField(max_digits=20, decimal_places=4)
    recoverable_amount = models.DecimalField(max_digits=20, decimal_places=4)
    hypothetical_carrying_amount = models.DecimalField(max_digits=20, decimal_places=4)
    reversal_amount = models.DecimalField(max_digits=20, decimal_places=4)
    reversal_income_account = models.ForeignKey(
        ChartOfAccount, on_delete=models.PROTECT, related_name="fixed_asset_impairment_reversal_income",
    )
    journal_entry = models.OneToOneField(
        JournalEntry, on_delete=models.PROTECT, related_name="asset_impairment_reversal",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("reversal_date", "id")
        constraints = [
            models.UniqueConstraint(fields=("asset", "reversal_date"), name="uq_asset_impairment_reversal_date"),
            models.CheckConstraint(condition=Q(carrying_amount_before__gte=0), name="ck_asset_reversal_carrying_nonnegative"),
            models.CheckConstraint(condition=Q(recoverable_amount__gte=0), name="ck_asset_reversal_recoverable_nonnegative"),
            models.CheckConstraint(condition=Q(hypothetical_carrying_amount__gte=0), name="ck_asset_reversal_ceiling_nonnegative"),
            models.CheckConstraint(condition=Q(reversal_amount__gt=0), name="ck_asset_reversal_amount_positive"),
        ]

    def clean(self):
        super().clean()
        if self.reversal_amount > min(
            self.recoverable_amount - self.carrying_amount_before,
            self.hypothetical_carrying_amount - self.carrying_amount_before,
        ):
            raise ValidationError({"reversal_amount": "عكس الانخفاض يتجاوز الحد المسموح وفق IAS 36."})
        account = self.reversal_income_account
        if account and (
            account.company_id != self.asset.company_id or account.nature != "revenue"
            or account.is_group or not account.is_active or not account.allow_posting
        ):
            raise ValidationError({"reversal_income_account": "اختر حساب إيراد تفصيليًا نشطًا وقابلًا للترحيل من نفس الشركة."})

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("سجل عكس انخفاض القيمة بعد ترحيله غير قابل للتعديل.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("لا يمكن حذف عكس انخفاض قيمة مرتبط بقيد؛ استخدم إجراءً محاسبيًا معتمدًا.")


class AssetImpairmentReversalAllocation(models.Model):
    """Tracks the exact impairment layer and contra-account reduced by each reversal."""
    reversal = models.ForeignKey(AssetImpairmentReversal, on_delete=models.PROTECT, related_name="allocations")
    impairment = models.ForeignKey(AssetImpairment, on_delete=models.PROTECT, related_name="reversal_allocations")
    amount = models.DecimalField(max_digits=20, decimal_places=4)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("reversal", "impairment"), name="uq_asset_reversal_impairment"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="ck_asset_reversal_allocation_positive"),
        ]

    def clean(self):
        super().clean()
        if self.reversal_id and self.impairment_id and self.reversal.asset_id != self.impairment.asset_id:
            raise ValidationError("طبقة الانخفاض لا تتبع الأصل المرتبط بعكس الانخفاض.")
        if self.amount <= 0:
            raise ValidationError({"amount": "قيمة التخصيص يجب أن تكون أكبر من صفر."})
        if self.impairment_id:
            already_allocated = self.impairment.reversal_allocations.filter(
                reversal__journal_entry__status=JournalEntry.Status.POSTED,
            ).exclude(pk=self.pk).aggregate(total=models.Sum("amount"))["total"] or 0
            if already_allocated + self.amount > self.impairment.impairment_loss:
                raise ValidationError({"amount": "إجمالي عكوس طبقة الانخفاض لا يجوز أن يتجاوز خسارة الانخفاض الأصلية."})

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("تخصيص عكس الانخفاض غير قابل للتعديل بعد إنشائه.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("لا يمكن حذف تخصيص عكس انخفاض مرتبط بقيد مرحّل.")


class AssetPurchaseInvoice(models.Model):
    """فاتورة مورد مستقلة لاقتناء أصل ثابت خارج دورة استلام المخزون."""
    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        POSTED = "posted", "مرحّلة"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="asset_purchase_invoices")
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="asset_purchase_invoices")
    asset = models.OneToOneField(FixedAsset, on_delete=models.PROTECT, related_name="vendor_invoice")
    supplier = models.ForeignKey("suppliers.Supplier", on_delete=models.PROTECT, related_name="asset_purchase_invoices")
    fiscal_year = models.ForeignKey("companies.FiscalYear", on_delete=models.PROTECT, related_name="asset_purchase_invoices")
    invoice_number = models.CharField(max_length=80)
    invoice_date = models.DateField()
    net_amount = models.DecimalField(max_digits=20, decimal_places=4)
    tax_code = models.ForeignKey("accounting.TaxCode", null=True, blank=True, on_delete=models.PROTECT, related_name="asset_purchase_invoices")
    tax_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    recoverable_tax_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    nonrecoverable_tax_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    grand_total = models.DecimalField(max_digits=20, decimal_places=4)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    journal_entry = models.OneToOneField(JournalEntry, null=True, blank=True, on_delete=models.PROTECT, related_name="asset_purchase_invoice")
    created_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="created_asset_purchase_invoices")
    created_at = models.DateTimeField(auto_now_add=True)
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-invoice_date", "-id")
        constraints = [
            models.UniqueConstraint(fields=("company", "invoice_number"), name="uq_asset_invoice_company_no"),
            models.CheckConstraint(condition=Q(net_amount__gt=0), name="ck_asset_invoice_net_positive"),
            models.CheckConstraint(condition=Q(tax_amount__gte=0) & Q(recoverable_tax_amount__gte=0) & Q(nonrecoverable_tax_amount__gte=0), name="ck_asset_invoice_tax_nonnegative"),
            models.CheckConstraint(condition=Q(grand_total__gt=0), name="ck_asset_invoice_total_positive"),
            models.CheckConstraint(
                condition=Q(tax_amount=models.F("recoverable_tax_amount") + models.F("nonrecoverable_tax_amount")),
                name="ck_asset_invoice_tax_split",
            ),
            models.CheckConstraint(
                condition=Q(grand_total=models.F("net_amount") + models.F("tax_amount")),
                name="ck_asset_invoice_total_math",
            ),
        ]
        indexes = [models.Index(fields=("company", "supplier", "status", "invoice_date"), name="asset_inv_supplier_idx")]

    @property
    def company_id_for_permission(self):
        return self.company_id

    def clean(self):
        super().clean()
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "الفرع لا يتبع الشركة."})
        if self.asset_id and (self.asset.company_id != self.company_id or self.asset.branch_id != self.branch_id):
            raise ValidationError({"asset": "الأصل لا يتبع الشركة والفرع المحددين."})
        if self.supplier_id and self.supplier.company_id != self.company_id:
            raise ValidationError({"supplier": "المورد لا يتبع الشركة المحددة."})
        if self.fiscal_year_id and self.fiscal_year.company_id != self.company_id:
            raise ValidationError({"fiscal_year": "السنة المالية لا تتبع الشركة."})
        if self.tax_amount != self.recoverable_tax_amount + self.nonrecoverable_tax_amount:
            raise ValidationError({"tax_amount": "إجمالي الضريبة يجب أن يساوي القابل للاسترداد وغير القابل للاسترداد."})
        if self.grand_total != self.net_amount + self.tax_amount:
            raise ValidationError({"grand_total": "إجمالي الفاتورة غير مطابق للصافي والضريبة."})
        if self.tax_code_id and (
            self.tax_code.company_id != self.company_id or not self.tax_code.is_active
            or self.tax_code.country != self.company.country
            or self.tax_code.effective_from > self.invoice_date
            or (self.tax_code.effective_to and self.tax_code.effective_to < self.invoice_date)
        ):
            raise ValidationError({"tax_code": "كود الضريبة غير صالح للشركة أو تاريخ الفاتورة."})

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk, status=self.Status.POSTED).exists():
            raise ValidationError("فاتورة الأصل المرحلة غير قابلة للتعديل.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status == self.Status.POSTED:
            raise ValidationError("لا يمكن حذف فاتورة أصل مرحلة.")
        return super().delete(*args, **kwargs)


class AssetSupplierPayment(models.Model):
    """سند سداد مستقل مخصص لفاتورة شراء الأصل الثابت."""
    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        POSTED = "posted", "مرحّل"
        REVERSED = "reversed", "معكوس"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="asset_supplier_payments")
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="asset_supplier_payments")
    invoice = models.ForeignKey(AssetPurchaseInvoice, on_delete=models.PROTECT, related_name="payments")
    supplier = models.ForeignKey("suppliers.Supplier", on_delete=models.PROTECT, related_name="asset_payments")
    payment_number = models.CharField(max_length=40)
    payment_date = models.DateField()
    payment_account = models.ForeignKey(ChartOfAccount, on_delete=models.PROTECT, related_name="asset_supplier_payments")
    amount = models.DecimalField(max_digits=20, decimal_places=4)
    reference = models.CharField(max_length=120, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    journal_entry = models.OneToOneField(JournalEntry, null=True, blank=True, on_delete=models.PROTECT, related_name="asset_supplier_payment")
    created_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="created_asset_supplier_payments")
    created_at = models.DateTimeField(auto_now_add=True)
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-payment_date", "-id")
        constraints = [
            models.UniqueConstraint(fields=("company", "payment_number"), name="uq_asset_payment_company_no"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="ck_asset_payment_amount_positive"),
        ]
        indexes = [models.Index(fields=("company", "supplier", "status", "payment_date"), name="asset_pay_supplier_idx")]

    @property
    def company_id_for_permission(self):
        return self.company_id

    def clean(self):
        super().clean()
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "الفرع لا يتبع الشركة."})
        if self.invoice_id and (
            self.invoice.company_id != self.company_id or self.invoice.branch_id != self.branch_id
            or self.invoice.supplier_id != self.supplier_id
        ):
            raise ValidationError({"invoice": "فاتورة الأصل لا تطابق الشركة والفرع والمورد."})
        if self.supplier_id and self.supplier.company_id != self.company_id:
            raise ValidationError({"supplier": "المورد لا يتبع الشركة."})
        if self.payment_account_id and (
            self.payment_account.company_id != self.company_id or self.payment_account.nature != "asset"
            or not self.payment_account.is_active or self.payment_account.is_group
            or not self.payment_account.allow_posting
        ):
            raise ValidationError({"payment_account": "اختر حساب بنك أو خزينة نشطًا وقابلًا للترحيل."})

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk, status__in=(self.Status.POSTED, self.Status.REVERSED)).exists():
            raise ValidationError("سند السداد المرحّل غير قابل للتعديل.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status in (self.Status.POSTED, self.Status.REVERSED):
            raise ValidationError("لا يمكن حذف سند سداد مرحّل.")
        return super().delete(*args, **kwargs)



class AssetMovement(models.Model):
    """مستند معتمد يحفظ التاريخ الكامل لنقل الأصل أو تغيير عهدته وموقعه."""

    class MovementType(models.TextChoices):
        BRANCH_TRANSFER = "branch_transfer", "نقل بين الفروع"
        CUSTODY_CHANGE = "custody_change", "تغيير العهدة"
        LOCATION_CHANGE = "location_change", "تغيير الموقع"

    class Status(models.TextChoices):
        PENDING = "pending", "بانتظار الاعتماد"
        APPROVED = "approved", "معتمد"
        REJECTED = "rejected", "مرفوض"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="asset_movements")
    asset = models.ForeignKey(FixedAsset, on_delete=models.PROTECT, related_name="movements")
    movement_type = models.CharField(max_length=24, choices=MovementType.choices)
    movement_date = models.DateField()
    from_branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="asset_movements_from")
    to_branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="asset_movements_to")
    from_employee = models.ForeignKey("hr.Employee", null=True, blank=True, on_delete=models.PROTECT, related_name="asset_movements_from")
    to_employee = models.ForeignKey("hr.Employee", null=True, blank=True, on_delete=models.PROTECT, related_name="asset_movements_to")
    from_custodian = models.CharField(max_length=180, blank=True)
    to_custodian = models.CharField(max_length=180, blank=True)
    from_location = models.CharField(max_length=180, blank=True)
    to_location = models.CharField(max_length=180, blank=True)
    document_number = models.CharField(max_length=80)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    requested_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="requested_asset_movements")
    reviewed_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="reviewed_asset_movements")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-movement_date", "-id")
        constraints = [
            models.UniqueConstraint(fields=("company", "document_number"), name="uq_asset_movement_company_doc"),
        ]
        indexes = [
            models.Index(fields=("company", "movement_date"), name="asset_move_co_date_idx"),
            models.Index(fields=("asset", "movement_date"), name="asset_move_asset_date_idx"),
        ]

    def clean(self):
        super().clean()
        if self.asset_id and self.asset.company_id != self.company_id:
            raise ValidationError({"asset": "الأصل لا يتبع الشركة المحددة."})
        for field in ("from_branch", "to_branch"):
            branch = getattr(self, field, None)
            if branch and branch.company_id != self.company_id:
                raise ValidationError({field: "الفرع يجب أن يتبع نفس الشركة."})
        for field in ("from_employee", "to_employee"):
            employee = getattr(self, field, None)
            if employee and employee.company_id != self.company_id:
                raise ValidationError({field: "الموظف يجب أن يتبع نفس الشركة."})
        if self.from_employee_id and self.from_employee.branch_id != self.from_branch_id:
            raise ValidationError({"from_employee": "الموظف السابق يجب أن يتبع فرع الأصل السابق."})
        if self.to_employee_id and self.to_employee.branch_id != self.to_branch_id:
            raise ValidationError({"to_employee": "الموظف المستلم يجب أن يتبع الفرع المستلم للأصل."})
        if self.movement_type == self.MovementType.BRANCH_TRANSFER and self.from_branch_id == self.to_branch_id:
            raise ValidationError({"to_branch": "اختر فرعًا مختلفًا عن الفرع الحالي."})
        if self.movement_type != self.MovementType.BRANCH_TRANSFER and self.from_branch_id != self.to_branch_id:
            raise ValidationError({"to_branch": "تغيير العهدة أو الموقع لا يغيّر فرع الأصل."})
        if self.asset_id and self.movement_date < self.asset.acquisition_date:
            raise ValidationError({"movement_date": "تاريخ الحركة لا يسبق تاريخ اقتناء الأصل."})

    def save(self, *args, **kwargs):
        if self.pk:
            previous_status = type(self).objects.filter(pk=self.pk).values_list("status", flat=True).first()
            if previous_status and previous_status != self.Status.PENDING:
                raise ValidationError("مستند حركة الأصل بعد مراجعته غير قابل للتعديل.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("لا يمكن حذف مستند حركة أصل؛ استخدم مستندًا عكسيًا معتمدًا عند الحاجة.")


class AssetComponent(models.Model):
    """مكوّن جوهري من الأصل له تكلفة وعمر وطريقة إهلاك مستقلة ضمن سجل الأصل الأم."""
    class Status(models.TextChoices):
        ACTIVE = "active", "نشط"
        DISPOSED = "disposed", "مستبعد"

    asset = models.ForeignKey(FixedAsset, on_delete=models.PROTECT, related_name="components")
    code = models.CharField(max_length=40)
    name_ar = models.CharField(max_length=180)
    original_cost = models.DecimalField(max_digits=20, decimal_places=4)
    salvage_value = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    useful_life_months = models.PositiveIntegerField()
    depreciation_method = models.CharField(max_length=24, choices=FixedAsset.Method.choices, default=FixedAsset.Method.STRAIGHT_LINE)
    declining_rate_percentage = models.DecimalField(max_digits=7, decimal_places=4, null=True, blank=True)
    production_units_total = models.PositiveBigIntegerField(null=True, blank=True)
    asset_account = models.ForeignKey(ChartOfAccount, on_delete=models.PROTECT, related_name="fixed_asset_component_costs")
    accumulated_depreciation_account = models.ForeignKey(ChartOfAccount, on_delete=models.PROTECT, related_name="fixed_asset_component_accumulated")
    depreciation_expense_account = models.ForeignKey(ChartOfAccount, on_delete=models.PROTECT, related_name="fixed_asset_component_expenses")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("code",)
        constraints = [
            models.UniqueConstraint(fields=("asset", "code"), name="uq_asset_component_code"),
            models.CheckConstraint(condition=Q(original_cost__gt=0), name="ck_asset_component_cost_positive"),
            models.CheckConstraint(condition=Q(salvage_value__gte=0), name="ck_asset_component_salvage_nonnegative"),
            models.CheckConstraint(condition=Q(useful_life_months__gt=0), name="ck_asset_component_life_positive"),
        ]

    def clean(self):
        super().clean()
        if self.salvage_value > self.original_cost:
            raise ValidationError({"salvage_value": "القيمة التخريدية للمكوّن لا تتجاوز تكلفته."})
        parent_accounts = {
            "asset_account": self.asset.asset_account_id,
            "accumulated_depreciation_account": self.asset.accumulated_depreciation_account_id,
            "depreciation_expense_account": self.asset.depreciation_expense_account_id,
        }
        for field, parent_account_id in parent_accounts.items():
            account = getattr(self, field, None)
            if account and account.company_id != self.asset.company_id:
                raise ValidationError({field: "حساب المكوّن يجب أن يتبع شركة الأصل."})
            if account and account.pk != parent_account_id:
                raise ValidationError({field: "يجب استخدام حسابات الأصل الأم لضمان مطابقة دفتر الأستاذ وسجل المكونات."})
        if self.depreciation_method == FixedAsset.Method.DECLINING_BALANCE:
            if not self.declining_rate_percentage or not (0 < self.declining_rate_percentage <= 100):
                raise ValidationError({"declining_rate_percentage": "أدخل معدل قسط متناقص أكبر من صفر وحتى 100%."})
        elif self.declining_rate_percentage is not None:
            raise ValidationError({"declining_rate_percentage": "معدل القسط المتناقص يستخدم مع طريقته فقط."})
        if self.depreciation_method == FixedAsset.Method.UNITS_OF_PRODUCTION and not self.production_units_total:
            raise ValidationError({"production_units_total": "حدد إجمالي وحدات الإنتاج المقدرة."})


    @property
    def accumulated_depreciation(self):
        return self.depreciations.filter(
            journal_entry__status=JournalEntry.Status.POSTED,
        ).aggregate(total=models.Sum("amount"))["total"] or 0


class AssetInsurancePolicy(models.Model):
    """وثيقة تأمين الأصل كسجل رقابي لا ينشئ قيدًا محاسبيًا تلقائيًا."""
    class Status(models.TextChoices):
        ACTIVE = "active", "سارية"
        EXPIRED = "expired", "منتهية"
        CANCELLED = "cancelled", "ملغاة"

    asset = models.ForeignKey(FixedAsset, on_delete=models.PROTECT, related_name="insurance_policies")
    policy_number = models.CharField(max_length=100)
    insurer_name = models.CharField(max_length=180)
    coverage_description = models.TextField(blank=True)
    start_date = models.DateField()
    end_date = models.DateField()
    insured_value = models.DecimalField(max_digits=20, decimal_places=4)
    premium_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    document_reference = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-end_date", "-id")
        constraints = [
            models.UniqueConstraint(fields=("asset", "policy_number"), name="uq_asset_insurance_policy"),
            models.CheckConstraint(condition=Q(end_date__gte=models.F("start_date")), name="ck_asset_insurance_dates"),
            models.CheckConstraint(condition=Q(insured_value__gte=0), name="ck_asset_insured_value_nonnegative"),
            models.CheckConstraint(condition=Q(premium_amount__gte=0), name="ck_asset_insurance_premium_nonnegative"),
        ]


class AssetPhysicalInventory(models.Model):
    """محضر جرد فعلي للأصل ومقارنة موقعه وعهدته وحالته دون أثر مالي تلقائي."""
    class Result(models.TextChoices):
        MATCHED = "matched", "مطابق"
        LOCATION_MISMATCH = "location_mismatch", "اختلاف موقع"
        CUSTODIAN_MISMATCH = "custodian_mismatch", "اختلاف عهدة"
        CONDITION_ISSUE = "condition_issue", "ملاحظة حالة"
        NOT_FOUND = "not_found", "غير موجود"

    asset = models.ForeignKey(FixedAsset, on_delete=models.PROTECT, related_name="physical_inventory")
    count_date = models.DateField()
    expected_location = models.CharField(max_length=180, blank=True)
    counted_location = models.CharField(max_length=180, blank=True)
    expected_custodian = models.CharField(max_length=180, blank=True)
    counted_custodian = models.CharField(max_length=180, blank=True)
    result = models.CharField(max_length=24, choices=Result.choices)
    condition_notes = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    counted_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="asset_inventory_counts")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-count_date", "-id")
        constraints = [
            models.UniqueConstraint(fields=("asset", "count_date"), name="uq_asset_physical_count_date"),
        ]


class AssetComponentDepreciation(models.Model):
    """قسط إهلاك مكوّن مستقل مرتبط بقيد الفترة المجمّع للأصل."""
    component = models.ForeignKey(AssetComponent, on_delete=models.PROTECT, related_name="depreciations")
    period_date = models.DateField()
    amount = models.DecimalField(max_digits=20, decimal_places=4)
    units_produced = models.PositiveBigIntegerField(null=True, blank=True)
    journal_entry = models.ForeignKey(JournalEntry, on_delete=models.PROTECT, related_name="asset_component_depreciations")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("period_date", "component_id")
        constraints = [
            models.UniqueConstraint(fields=("component", "period_date"), name="uq_asset_component_depr_period"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="ck_asset_component_depr_positive"),
        ]

