"""نماذج الشركات والعملات والفروع والسنوات المالية وعضويات المستخدمين."""

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

class Currency(models.Model):
    """يعرّف العملة ورمزها وبيانات عرضها."""
    code = models.CharField(max_length=3, unique=True)
    name_ar = models.CharField(max_length=80)
    name_en = models.CharField(max_length=80, blank=True)
    symbol = models.CharField(max_length=8, blank=True)
    decimal_places = models.PositiveSmallIntegerField(default=2)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("code",)
        verbose_name = "عملة"
        verbose_name_plural = "العملات"

    def __str__(self):
        return f"{self.code} - {self.name_ar}"


class Company(models.Model):
    """يحفظ هوية الشركة وإعداداتها المالية والتشغيلية."""
    class Country(models.TextChoices):
        EGYPT = "EG", "مصر"
        SAUDI_ARABIA = "SA", "السعودية"

    class Activity(models.TextChoices):
        COMMERCIAL = "commercial", "تجاري"
        SERVICE = "service", "خدمي"
        MEDICAL = "medical", "طبي"
        RESTAURANTS = "restaurants", "مطاعم"
        INDUSTRIAL = "industrial", "صناعي"
        TRANSPORT = "transport", "نقل وشحن"
        CONTRACTING = "contracting", "مقاولات"

    code = models.CharField(max_length=30, unique=True)
    name_ar = models.CharField(max_length=180)
    name_en = models.CharField(max_length=180, blank=True)
    country = models.CharField(max_length=2, choices=Country.choices)
    activity = models.CharField(max_length=24, choices=Activity.choices)
    base_currency = models.ForeignKey(Currency, on_delete=models.PROTECT, related_name="companies")
    tax_registration_number = models.CharField(max_length=80, blank=True)
    address = models.TextField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name_ar",)
        verbose_name = "شركة"
        verbose_name_plural = "الشركات"

    def __str__(self):
        return f"{self.code} - {self.name_ar}"


class CompanyActivity(models.Model):
    """نشاط تشغيلي مفعّل للشركة؛ يحتفظ Company.activity بالنشاط الأساسي للتوافق."""

    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="activity_assignments"
    )
    activity = models.CharField(max_length=24, choices=Company.Activity.choices)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("activity",)
        indexes = [models.Index(fields=("company", "is_active"), name="company_activity_active_idx")]
        constraints = [
            models.UniqueConstraint(
                fields=("company", "activity"), name="uq_company_enabled_activity"
            ),
        ]
        verbose_name = "نشاط شركة"
        verbose_name_plural = "أنشطة الشركات"

    def __str__(self):
        return f"{self.company.code} - {self.get_activity_display()}"


class ExchangeRate(models.Model):
    """سعر وحدة واحدة من العملة الأجنبية بوحدات العملة الأساسية للشركة."""
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="exchange_rates")
    currency = models.ForeignKey(Currency, on_delete=models.PROTECT, related_name="exchange_rates")
    rate_date = models.DateField()
    rate = models.DecimalField(max_digits=20, decimal_places=10)
    source = models.CharField(max_length=120, blank=True)
    notes = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-rate_date", "currency__code")
        constraints = [
            models.UniqueConstraint(fields=("company", "currency", "rate_date"), name="uq_exchange_rate_company_currency_date"),
            models.CheckConstraint(condition=Q(rate__gt=0), name="ck_exchange_rate_positive"),
        ]
        indexes = [models.Index(fields=("company", "currency", "-rate_date"), name="fx_co_cur_date_idx")]
        verbose_name = "سعر صرف"
        verbose_name_plural = "أسعار الصرف"

    def clean(self):
        super().clean()
        if self.rate is not None and self.rate <= 0:
            raise ValidationError({"rate": "سعر الصرف يجب أن يكون أكبر من صفر."})
        if self.company_id and self.currency_id:
            if self.currency.code == self.company.base_currency.code:
                raise ValidationError({"currency": "لا يلزم تسجيل سعر صرف للعملة الأساسية للشركة."})
            if not self.currency.is_active:
                raise ValidationError({"currency": "لا يمكن تسجيل سعر صرف لعملة موقوفة."})

    def __str__(self):
        return f"{self.currency.code} / {self.company.base_currency.code} - {self.rate_date}: {self.rate}"


class Branch(models.Model):
    """يمثل فرعًا تابعًا للشركة ويحدد الفرع الرئيسي عند الحاجة."""
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="branches")
    code = models.CharField(max_length=30)
    name_ar = models.CharField(max_length=160)
    address = models.TextField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    is_head_office = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("company__code", "code")
        constraints = [
            models.UniqueConstraint(fields=("company", "code"), name="uq_branch_company_code"),
            models.UniqueConstraint(
                fields=("company",), condition=Q(is_head_office=True),
                name="uq_company_single_head_office",
            ),
        ]
        verbose_name = "فرع"
        verbose_name_plural = "الفروع"

    def __str__(self):
        return f"{self.company.code}/{self.code} - {self.name_ar}"


class FiscalYear(models.Model):
    """يحدد حدود السنة المالية وحالة فتحها أو إغلاقها."""
    class Status(models.TextChoices):
        OPEN = "open", "مفتوحة"
        CLOSED = "closed", "مغلقة"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="fiscal_years")
    name = models.CharField(max_length=80)
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.OPEN)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-start_date",)
        constraints = [
            models.UniqueConstraint(fields=("company", "name"), name="uq_fiscal_year_company_name"),
            models.CheckConstraint(condition=Q(end_date__gte=models.F("start_date")), name="ck_fiscal_year_dates"),
        ]
        verbose_name = "سنة مالية"
        verbose_name_plural = "السنوات المالية"

    def clean(self):
        super().clean()
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "تاريخ نهاية السنة المالية يجب أن يكون بعد تاريخ البداية."})

    def __str__(self):
        return f"{self.company.code} - {self.name}"


class FiscalPeriod(models.Model):
    """يمثل فترة محاسبية شهرية داخل سنة مالية مع سجل آخر إقفال وإعادة فتح."""
    class Status(models.TextChoices):
        OPEN = "open", "مفتوحة"
        CLOSED = "closed", "مغلقة"

    fiscal_year = models.ForeignKey(FiscalYear, on_delete=models.PROTECT, related_name="periods")
    name = models.CharField(max_length=40)
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.OPEN)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="closed_fiscal_periods")
    reopened_at = models.DateTimeField(null=True, blank=True)
    reopened_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="reopened_fiscal_periods")
    reopen_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("start_date", "id")
        constraints = [
            models.UniqueConstraint(fields=("fiscal_year", "name"), name="uq_fiscal_period_year_name"),
            models.CheckConstraint(condition=Q(end_date__gte=models.F("start_date")), name="ck_fiscal_period_dates"),
        ]
        indexes = [
            models.Index(fields=("fiscal_year", "status", "start_date"), name="fiscal_period_status_idx"),
        ]

    @property
    def company_id(self):
        return self.fiscal_year.company_id

    def clean(self):
        super().clean()
        if self.start_date < self.fiscal_year.start_date or self.end_date > self.fiscal_year.end_date:
            raise ValidationError("الفترة الشهرية يجب أن تقع بالكامل داخل حدود السنة المالية.")
        overlapping = type(self).objects.filter(
            fiscal_year_id=self.fiscal_year_id,
            start_date__lte=self.end_date,
            end_date__gte=self.start_date,
        )
        if self.pk:
            overlapping = overlapping.exclude(pk=self.pk)
        if overlapping.exists():
            raise ValidationError("تتداخل الفترة الشهرية مع فترة أخرى في نفس السنة المالية.")

    def __str__(self):
        return f"{self.fiscal_year.name} - {self.name}"


class CompanyMembership(models.Model):
    """Explicit company and branch access for a user."""
    class Role(models.TextChoices):
        ADMIN = "admin", "مدير شركة"
        ACCOUNTANT = "accountant", "محاسب"
        VIEWER = "viewer", "مشاهد"

    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="company_memberships")
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.VIEWER)
    all_branches = models.BooleanField(default=False)
    branches = models.ManyToManyField(Branch, blank=True, related_name="user_memberships")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("user", "company"), name="uq_company_membership_user_company"),
        ]
        ordering = ("company__code", "user__username")
        indexes = [models.Index(fields=("user", "is_active", "company"), name="companies_c_user_id_1b1dae_idx")]
        verbose_name = "عضوية شركة"
        verbose_name_plural = "عضويات الشركات"

    def clean(self):
        super().clean()
        if self.role == self.Role.ADMIN and not self.all_branches:
            raise ValidationError({"all_branches": "مدير الشركة يجب أن يمتلك نطاق الوصول إلى جميع الفروع."})
        if self.pk and self.branches.exclude(company_id=self.company_id).exists():
            raise ValidationError({"branches": "كل الفروع المحددة يجب أن تتبع نفس الشركة."})

    def save(self, *args, **kwargs):
        if self.role == self.Role.ADMIN and not self.all_branches:
            raise ValidationError({"all_branches": "مدير الشركة يجب أن يمتلك نطاق الوصول إلى جميع الفروع."})
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user} - {self.company} ({self.get_role_display()})"


class PlatformOwner(models.Model):
    """هوية مستقلة لمالك المنصة، منفصلة عن عضويات الشركات."""
    user = models.OneToOneField(
        "accounts.User", on_delete=models.PROTECT,
        related_name="platform_owner_profile",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "مالك المنصة"
        verbose_name_plural = "مالكو المنصة"

    def __str__(self):
        return f"مالك المنصة: {self.user}"
