from django.core.validators import MinValueValidator
from django.db import models

from companies.models import Company


class Customer(models.Model):
    class CustomerType(models.TextChoices):
        COMPANY = "company", "شركة"
        INDIVIDUAL = "individual", "فرد"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="customers")
    code = models.CharField(max_length=40)
    customer_type = models.CharField(
        max_length=12, choices=CustomerType.choices, default=CustomerType.COMPANY
    )
    name_ar = models.CharField(max_length=180)
    name_en = models.CharField(max_length=180, blank=True)
    tax_number = models.CharField(max_length=80, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    mobile = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    credit_limit = models.DecimalField(
        max_digits=16, decimal_places=2, default=0,
        validators=[MinValueValidator(0)],
    )
    payment_terms_days = models.PositiveSmallIntegerField(default=0)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name_ar", "code")
        constraints = [
            models.UniqueConstraint(fields=("company", "code"), name="uq_customer_company_code"),
        ]
        indexes = [
            models.Index(fields=("company", "is_active", "name_ar"), name="customer_company_active_name_idx"),
            models.Index(fields=("company", "tax_number"), name="customer_company_tax_idx"),
        ]
        verbose_name = "عميل"
        verbose_name_plural = "العملاء"

    def __str__(self):
        return f"{self.code} - {self.name_ar}"
