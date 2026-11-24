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
    serial_number = models.CharField(max_length=100, blank=True)
    warranty_end_date = models.DateField(null=True, blank=True)
    supplier_name = models.CharField(max_length=180, blank=True)
    supplier_invoice = models.CharField(max_length=100, blank=True)
    acquisition_date = models.DateField()
    depreciation_start_date = models.DateField()
    original_cost = models.DecimalField(max_digits=20, decimal_places=4)
    acquisition_net_amount = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    nonrecoverable_tax_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    acquisition_tax_code = models.ForeignKey(
        "accounting.TaxCode", null=True, blank=True, on_delete=models.PROTECT,
        related_name="fixed_asset_acquisitions",
    )
    salvage_value = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    useful_life_months = models.PositiveIntegerField()
    depreciation_method = models.CharField(max_length=20, choices=Method.choices, default=Method.STRAIGHT_LINE)
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
        if self.salvage_value > self.original_cost:
            raise ValidationError({"salvage_value": "القيمة التخريدية لا يمكن أن تتجاوز التكلفة."})
        if self.nonrecoverable_tax_amount < 0:
            raise ValidationError({"nonrecoverable_tax_amount": "قيمة الضريبة غير القابلة للخصم لا يمكن أن تكون سالبة."})
        if self.acquisition_net_amount is not None:
            if self.acquisition_net_amount < 0 or self.acquisition_net_amount + self.nonrecoverable_tax_amount != self.original_cost:
                raise ValidationError({"original_cost": "تكلفة الأصل يجب أن تساوي صافي الاقتناء مضافًا إليه الضريبة غير القابلة للخصم."})
        elif self.nonrecoverable_tax_amount:
            raise ValidationError({"acquisition_net_amount": "أدخل صافي تكلفة الاقتناء لتوثيق الضريبة غير القابلة للخصم."})
        for field in ("asset_account", "accumulated_depreciation_account", "depreciation_expense_account", "acquisition_counter_account", "disposal_gain_loss_account", "disposal_loss_account"):
            account = getattr(self, field, None)
            if account and account.company_id != self.company_id:
                raise ValidationError({field: "الحساب يجب أن يتبع نفس الشركة."})
        if self.depreciation_start_date < self.acquisition_date:
            raise ValidationError({"depreciation_start_date": "بداية الإهلاك لا تسبق تاريخ الاقتناء."})

    @property
    def accumulated_depreciation(self):
        return self.depreciations.aggregate(total=models.Sum("amount"))["total"] or 0

    @property
    def net_book_value(self):
        return self.original_cost - self.accumulated_depreciation

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
    journal_entry = models.OneToOneField(JournalEntry, on_delete=models.PROTECT, related_name="asset_depreciation")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("period_date",)
        constraints = [models.UniqueConstraint(fields=("asset", "period_date"), condition=Q(is_partial_period=False), name="uq_asset_depr_period")]
