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



class StockMovement(models.Model):
    class MovementType(models.TextChoices):
        RECEIPT = "receipt", "استلام"
        ISSUE = "issue", "صرف"
        TRANSFER = "transfer", "تحويل"
        ADJUSTMENT = "adjustment", "تسوية جرد"

    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        POSTED = "posted", "مرحّل"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="stock_movements")
    movement_number = models.CharField(max_length=40)
    movement_type = models.CharField(max_length=12, choices=MovementType.choices)
    movement_date = models.DateField()
    source_warehouse = models.ForeignKey(Warehouse, null=True, blank=True, on_delete=models.PROTECT, related_name="outgoing_movements")
    destination_warehouse = models.ForeignKey(Warehouse, null=True, blank=True, on_delete=models.PROTECT, related_name="incoming_movements")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="created_stock_movements")
    posted_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="posted_stock_movements")
    posted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-movement_date", "-id")
        constraints = [
            models.UniqueConstraint(fields=("company", "movement_number"), name="uq_stock_movement_company_number"),
        ]
        indexes = [
            models.Index(fields=("company", "status", "movement_date"), name="inv_move_status_date_idx"),
        ]

    def clean(self):
        super().clean()
        for warehouse in (self.source_warehouse, self.destination_warehouse):
            if warehouse and warehouse.company_id != self.company_id:
                raise ValidationError("كل المخازن يجب أن تتبع نفس الشركة.")
        if self.movement_type == self.MovementType.RECEIPT and not self.destination_warehouse_id:
            raise ValidationError({"destination_warehouse": "حدد مخزن الاستلام."})
        if self.movement_type == self.MovementType.ISSUE and not self.source_warehouse_id:
            raise ValidationError({"source_warehouse": "حدد مخزن الصرف."})
        if self.movement_type == self.MovementType.TRANSFER:
            if not self.source_warehouse_id or not self.destination_warehouse_id:
                raise ValidationError("التحويل يتطلب مخزن مصدر ومخزن وجهة.")
            if self.source_warehouse_id == self.destination_warehouse_id:
                raise ValidationError("لا يمكن التحويل إلى نفس المخزن.")
        if self.movement_type == self.MovementType.ADJUSTMENT and bool(self.source_warehouse_id) == bool(self.destination_warehouse_id):
            raise ValidationError("التسوية تتطلب تحديد مخزن واحد فقط.")


class StockMovementLine(models.Model):
    class AdjustmentDirection(models.TextChoices):
        IN = "in", "زيادة"
        OUT = "out", "عجز"

    movement = models.ForeignKey(StockMovement, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="stock_movement_lines")
    source_location = models.ForeignKey("StorageLocation", null=True, blank=True, on_delete=models.PROTECT, related_name="source_stock_lines")
    destination_location = models.ForeignKey("StorageLocation", null=True, blank=True, on_delete=models.PROTECT, related_name="destination_stock_lines")
    expiry_date = models.DateField(null=True, blank=True)
    quantity = models.DecimalField(max_digits=18, decimal_places=4, validators=[MinValueValidator(0.0001)])
    unit_cost = models.DecimalField(max_digits=18, decimal_places=4, default=0, validators=[MinValueValidator(0)])
    adjustment_direction = models.CharField(max_length=3, choices=AdjustmentDirection.choices, blank=True)

    class Meta:
        ordering = ("id",)

    def clean(self):
        super().clean()
        if self.product_id and self.movement_id and self.product.company_id != self.movement.company_id:
            raise ValidationError({"product": "الصنف لا يتبع شركة الحركة."})
        if self.movement_id and self.movement.status == StockMovement.Status.POSTED:
            raise ValidationError("لا يمكن تعديل سطور حركة مرحلة.")
        if self.movement_id and self.movement.movement_type == StockMovement.MovementType.ADJUSTMENT and not self.adjustment_direction:
            raise ValidationError({"adjustment_direction": "حدد الزيادة أو العجز للتسوية."})
        if self.movement_id:
            if self.source_location_id and self.source_location.warehouse_id != self.movement.source_warehouse_id:
                raise ValidationError({"source_location": "موقع الصرف لا يتبع مخزن المصدر."})
            if self.destination_location_id and self.destination_location.warehouse_id != self.movement.destination_warehouse_id:
                raise ValidationError({"destination_location": "موقع الاستلام لا يتبع مخزن الوجهة."})
            if self.product_id and self.product.track_expiry and not self.expiry_date:
                raise ValidationError({"expiry_date": "تاريخ الصلاحية مطلوب لهذا الصنف."})
            if self.product_id and not self.product.track_expiry and self.expiry_date:
                raise ValidationError({"expiry_date": "الصنف لا يتطلب تتبع الصلاحية."})


class StockBalance(models.Model):
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="stock_balances")
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name="stock_balances")
    location = models.ForeignKey(StorageLocation, null=True, blank=True, on_delete=models.PROTECT, related_name="stock_balances")
    expiry_date = models.DateField(null=True, blank=True)
    quantity = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    inventory_value = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    average_cost = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("product", "warehouse", "location", "expiry_date"), nulls_distinct=False, name="uq_stock_balance_product_warehouse_location_expiry"),
            models.CheckConstraint(condition=Q(quantity__gte=0), name="ck_stock_balance_nonnegative"),
            models.CheckConstraint(condition=Q(inventory_value__gte=0), name="ck_stock_balance_value_nonnegative"),
        ]
        indexes = [models.Index(fields=("warehouse", "product"), name="inv_balance_wh_product_idx")]




class StockCostLayer(models.Model):
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="cost_layers")
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name="cost_layers")
    source_line = models.ForeignKey(StockMovementLine, on_delete=models.PROTECT, related_name="created_cost_layers")
    location = models.ForeignKey(StorageLocation, null=True, blank=True, on_delete=models.PROTECT, related_name="fifo_cost_layers")
    expiry_date = models.DateField(null=True, blank=True)
    quantity_remaining = models.DecimalField(max_digits=20, decimal_places=4, validators=[MinValueValidator(0)])
    unit_cost = models.DecimalField(max_digits=20, decimal_places=6, validators=[MinValueValidator(0)])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "id")
        indexes = [models.Index(fields=("product", "warehouse", "location", "created_at"), name="inv_fifo_layer_order_idx")]
        constraints = [models.CheckConstraint(condition=Q(quantity_remaining__gte=0), name="ck_fifo_layer_nonnegative")]
