from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

class Currency(models.Model):
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
    class Country(models.TextChoices):
        EGYPT = "EG", "مصر"
        SAUDI_ARABIA = "SA", "السعودية"

    class Activity(models.TextChoices):
        COMMERCIAL = "commercial", "تجاري"
        SERVICE = "service", "خدمي"
        MEDICAL = "medical", "طبي"
        RESTAURANTS = "restaurants", "مطاعم"
        INDUSTRIAL = "industrial", "صناعي"

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


class Branch(models.Model):
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
