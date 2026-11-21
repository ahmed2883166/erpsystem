from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q

from companies.models import Branch, Company


class ProductCategory(models.Model):
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="product_categories")
    code = models.CharField(max_length=40)
    name_ar = models.CharField(max_length=160)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("code",)
        constraints = [models.UniqueConstraint(fields=("company", "code"), name="uq_product_category_company_code")]
        verbose_name = "مجموعة صنفية"
        verbose_name_plural = "المجموعات الصنفية"

    def clean(self):
        super().clean()
        if self.parent_id and self.parent.company_id != self.company_id:
            raise ValidationError({"parent": "المجموعة الأب يجب أن تتبع نفس الشركة."})
        if self.parent_id == self.pk and self.pk:
            raise ValidationError({"parent": "لا يمكن أن تكون المجموعة أبًا لنفسها."})


class UnitOfMeasure(models.Model):
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="inventory_units")
    code = models.CharField(max_length=24)
    name_ar = models.CharField(max_length=80)
    symbol = models.CharField(max_length=16, blank=True)
    decimal_places = models.PositiveSmallIntegerField(default=2)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("code",)
        constraints = [
            models.UniqueConstraint(fields=("company", "code"), name="uq_inventory_unit_company_code"),
            models.CheckConstraint(condition=Q(decimal_places__lte=6), name="ck_inventory_unit_precision"),
        ]
        verbose_name = "وحدة قياس"
        verbose_name_plural = "وحدات القياس"


class Product(models.Model):
    class ProductType(models.TextChoices):
        GOODS = "goods", "صنف مخزني"
        SERVICE = "service", "خدمة"

    class CostingMethod(models.TextChoices):
        WEIGHTED_AVERAGE = "weighted_average", "متوسط مرجح"
        FIFO = "fifo", "الوارد أولاً يصرف أولاً"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="products")
    category = models.ForeignKey(ProductCategory, null=True, blank=True, on_delete=models.PROTECT, related_name="products")
    unit = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT, related_name="products")
    code = models.CharField(max_length=50)
    barcode = models.CharField(max_length=80, blank=True)
    name_ar = models.CharField(max_length=180)
    name_en = models.CharField(max_length=180, blank=True)
    product_type = models.CharField(max_length=10, choices=ProductType.choices, default=ProductType.GOODS)
    purchase_price = models.DecimalField(max_digits=18, decimal_places=4, default=0, validators=[MinValueValidator(0)])
    sale_price = models.DecimalField(max_digits=18, decimal_places=4, default=0, validators=[MinValueValidator(0)])
    costing_method = models.CharField(max_length=20, choices=CostingMethod.choices, default=CostingMethod.WEIGHTED_AVERAGE)
    min_stock = models.DecimalField(max_digits=18, decimal_places=4, default=0, validators=[MinValueValidator(0)])
    track_expiry = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("code",)
        constraints = [
            models.UniqueConstraint(fields=("company", "code"), name="uq_product_company_code"),
            models.UniqueConstraint(fields=("company", "barcode"), condition=~Q(barcode=""), name="uq_product_company_barcode"),
        ]
        indexes = [
            models.Index(fields=("company", "product_type", "is_active"), name="inv_product_type_active_idx"),
            models.Index(fields=("company", "name_ar"), name="inv_product_name_idx"),
        ]
        verbose_name = "صنف"
        verbose_name_plural = "الأصناف"

    def clean(self):
        super().clean()
        if self.category_id and self.category.company_id != self.company_id:
            raise ValidationError({"category": "المجموعة الصنفية يجب أن تتبع نفس الشركة."})
        if self.unit_id and self.unit.company_id != self.company_id:
            raise ValidationError({"unit": "وحدة القياس يجب أن تتبع نفس الشركة."})
        if self.product_type == self.ProductType.SERVICE and self.track_expiry:
            raise ValidationError({"track_expiry": "لا يمكن تتبع صلاحية خدمة."})


class Warehouse(models.Model):
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="warehouses")
    branch = models.ForeignKey(Branch, null=True, blank=True, on_delete=models.PROTECT, related_name="warehouses")
    code = models.CharField(max_length=40)
    name_ar = models.CharField(max_length=160)
    address = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("code",)
        constraints = [models.UniqueConstraint(fields=("company", "code"), name="uq_warehouse_company_code")]
        indexes = [models.Index(fields=("company", "is_active"), name="inv_warehouse_active_idx")]
        verbose_name = "مخزن"
        verbose_name_plural = "المخازن"

    def clean(self):
        super().clean()
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "الفرع يجب أن يتبع نفس الشركة."})


class StorageLocation(models.Model):
    class LocationType(models.TextChoices):
        FLOOR = "floor", "دور"
        AREA = "area", "منطقة"
        RACK = "rack", "رف"
        BIN = "bin", "خانة"

    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name="locations")
    code = models.CharField(max_length=40)
    name_ar = models.CharField(max_length=120)
    location_type = models.CharField(max_length=8, choices=LocationType.choices, default=LocationType.AREA)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("warehouse__code", "code")
        constraints = [models.UniqueConstraint(fields=("warehouse", "code"), name="uq_storage_location_wh_code")]
        verbose_name = "موقع تخزين"
        verbose_name_plural = "مواقع التخزين"

    @property
    def company_id(self):
        return self.warehouse.company_id

    def clean(self):
        super().clean()
        if self.parent_id and self.parent.warehouse_id != self.warehouse_id:
            raise ValidationError({"parent": "الموقع الأب يجب أن يتبع نفس المخزن."})
        if self.parent_id == self.pk and self.pk:
            raise ValidationError({"parent": "لا يمكن أن يكون الموقع أبًا لنفسه."})
