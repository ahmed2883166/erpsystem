"""نموذج العملاء وبيانات الاتصال والائتمان الخاصة بكل شركة."""

from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models

from companies.models import Company
from inventory.models import Product, ProductCategory


class Customer(models.Model):
    """يحفظ بيانات العميل وشروط السداد والحد الائتماني وحالة التعامل."""
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
    default_discount_percent = models.DecimalField(
        max_digits=7, decimal_places=4, default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    discount_approval_limit = models.DecimalField(
        max_digits=7, decimal_places=4, null=True, blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Maximum customer discount allowed without manager approval.",
    )
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
            models.Index(fields=("company", "is_active", "name_ar"), name="cust_co_active_name_idx"),
            models.Index(fields=("company", "tax_number"), name="cust_co_tax_idx"),
        ]
        verbose_name = "عميل"
        verbose_name_plural = "العملاء"

    def __str__(self):
        return f"{self.code} - {self.name_ar}"



class CustomerDiscountRule(models.Model):
    """قواعد خصم تجارية للشركة؛ يطبق النظام قاعدة واحدة فقط على كل بند."""
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="customer_discount_rules")
    customer = models.ForeignKey(
        Customer, null=True, blank=True, on_delete=models.PROTECT,
        related_name="discount_rules",
    )
    customer_type = models.CharField(
        max_length=12, choices=Customer.CustomerType.choices, blank=True,
    )
    product = models.ForeignKey(
        Product, null=True, blank=True, on_delete=models.PROTECT,
        related_name="customer_discount_rules",
    )
    category = models.ForeignKey(
        ProductCategory, null=True, blank=True, on_delete=models.PROTECT,
        related_name="customer_discount_rules",
    )
    min_quantity = models.DecimalField(
        max_digits=18, decimal_places=4, null=True, blank=True,
        validators=[MinValueValidator(0)],
    )
    min_invoice_amount = models.DecimalField(
        max_digits=20, decimal_places=4, null=True, blank=True,
        validators=[MinValueValidator(0)],
        help_text="Minimum invoice amount in the company's base currency.",
    )
    discount_percent = models.DecimalField(
        max_digits=7, decimal_places=4,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    priority = models.PositiveSmallIntegerField(default=100)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    notes = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-priority", "-id")
        indexes = [
            models.Index(fields=("company", "is_active", "effective_from"), name="cust_disc_co_active_from_idx"),
            models.Index(fields=("company", "customer", "is_active"), name="cust_disc_co_customer_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(effective_to__isnull=True) | models.Q(effective_to__gte=models.F("effective_from")),
                name="ck_customer_discount_date_range",
            ),
            models.CheckConstraint(
                condition=models.Q(min_quantity__isnull=True) | models.Q(min_quantity__gt=0),
                name="ck_customer_discount_min_qty",
            ),
        ]

    def clean(self):
        super().clean()
        if self.customer_id and self.customer.company_id != self.company_id:
            raise ValidationError({"customer": "العميل يجب أن يتبع نفس الشركة."})
        if self.product_id and self.product.company_id != self.company_id:
            raise ValidationError({"product": "الصنف يجب أن يتبع نفس الشركة."})
        if self.category_id and self.category.company_id != self.company_id:
            raise ValidationError({"category": "المجموعة الصنفية يجب أن تتبع نفس الشركة."})
        if self.customer_type and self.customer_type not in Customer.CustomerType.values:
            raise ValidationError({"customer_type": "نوع العميل غير صالح."})
        if self.effective_to and self.effective_to < self.effective_from:
            raise ValidationError({"effective_to": "تاريخ النهاية يجب ألا يسبق تاريخ البداية."})
        if not (0 <= self.discount_percent <= 100):
            raise ValidationError({"discount_percent": "نسبة الخصم يجب أن تكون بين 0 و100."})
