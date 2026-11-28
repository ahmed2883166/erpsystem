"""نماذج الأصناف والمخازن ومواقع التخزين وحركات المخزون وتقييم التكلفة والربط المحاسبي."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q

from companies.models import Branch, Company


class ProductCategory(models.Model):
    """يمثل التصنيف الهرمي للأصناف داخل شركة واحدة."""
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
        if self.parent_id:
            visited = set()
            node = self.parent
            while node is not None:
                if self.pk and node.pk == self.pk:
                    raise ValidationError({"parent": "لا يمكن أن تكون المجموعة تابعة لأحد أبنائها."})
                if node.pk in visited:
                    raise ValidationError({"parent": "يوجد تسلسل أبوة دائري في التصنيفات."})
                visited.add(node.pk)
                node = node.parent


class UnitOfMeasure(models.Model):
    """يعرّف وحدة قياس الأصناف ودقة الكميات المسموح بها."""
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
    """يحفظ بيانات الصنف أو الخدمة وأسعارها وطريقة تقييم مخزونها."""
    class ProductType(models.TextChoices):
        GOODS = "goods", "صنف مخزني"
        SERVICE = "service", "خدمة"

    class CostingMethod(models.TextChoices):
        WEIGHTED_AVERAGE = "weighted_average", "متوسط مرجح"
        FIFO = "fifo", "الوارد أولاً يصرف أولاً"

    class StockClass(models.TextChoices):
        RAW_MATERIAL = "raw_material", "مواد خام"
        FINISHED_GOODS = "finished_goods", "منتجات تامة"
        OPERATING_SUPPLIES = "operating_supplies", "مستلزمات تشغيل"
        SPARE_PARTS = "spare_parts", "قطع غيار"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="products")
    category = models.ForeignKey(ProductCategory, null=True, blank=True, on_delete=models.PROTECT, related_name="products")
    unit = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT, related_name="products")
    code = models.CharField(max_length=50)
    barcode = models.CharField(max_length=80, blank=True)
    name_ar = models.CharField(max_length=180)
    name_en = models.CharField(max_length=180, blank=True)
    product_type = models.CharField(max_length=10, choices=ProductType.choices, default=ProductType.GOODS)
    stock_class = models.CharField(max_length=20, choices=StockClass.choices, default=StockClass.FINISHED_GOODS, null=True, blank=True)
    purchase_price = models.DecimalField(max_digits=18, decimal_places=4, default=0, validators=[MinValueValidator(0)])
    sale_price = models.DecimalField(max_digits=18, decimal_places=4, default=0, validators=[MinValueValidator(0)])
    unit_weight = models.DecimalField(max_digits=18, decimal_places=6, default=0, validators=[MinValueValidator(0)])
    unit_volume = models.DecimalField(max_digits=18, decimal_places=6, default=0, validators=[MinValueValidator(0)])
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
        if self.product_type == self.ProductType.SERVICE and self.stock_class is not None:
            raise ValidationError({"stock_class": "تصنيف المخزون لا يطبق على الخدمات."})
        if self.product_type == self.ProductType.GOODS and not self.stock_class:
            raise ValidationError({"stock_class": "حدد تصنيف الصنف المخزني."})


class Warehouse(models.Model):
    """يمثل مخزنًا تابعًا لشركة وفرع محدد عند الحاجة."""
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="warehouses")
    branch = models.ForeignKey(Branch, null=True, blank=True, on_delete=models.PROTECT, related_name="warehouses")
    code = models.CharField(max_length=40)
    name_ar = models.CharField(max_length=160)
    address = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    is_transit = models.BooleanField(default=False, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("code",)
        constraints = [
            models.UniqueConstraint(fields=("company", "code"), name="uq_warehouse_company_code"),
            models.UniqueConstraint(fields=("company",), condition=Q(is_transit=True), name="uq_warehouse_company_transit"),
        ]
        indexes = [models.Index(fields=("company", "is_active"), name="inv_warehouse_active_idx")]
        verbose_name = "مخزن"
        verbose_name_plural = "المخازن"

    def clean(self):
        super().clean()
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "الفرع يجب أن يتبع نفس الشركة."})
        if self.is_transit and self.branch_id:
            raise ValidationError({"branch": "مخزن البضاعة بالطريق لا يتبع فرعًا تشغيليًا محددًا."})


class WarehouseProductSetting(models.Model):
    """يضبط حد إعادة الطلب الخاص بصنف داخل مخزن بعينه."""
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="warehouse_settings")
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name="product_settings")
    min_stock = models.DecimalField(max_digits=18, decimal_places=4, default=0, validators=[MinValueValidator(0)])
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def company_id(self):
        return self.warehouse.company_id

    class Meta:
        ordering = ("warehouse__code", "product__code")
        constraints = [
            models.UniqueConstraint(fields=("product", "warehouse"), name="uq_wh_product_setting"),
        ]
        indexes = [models.Index(fields=("warehouse", "is_active"), name="inv_wh_setting_active_idx")]
        verbose_name = "حد إعادة طلب الصنف بالمخزن"
        verbose_name_plural = "حدود إعادة الطلب بالمخازن"

    def clean(self):
        super().clean()
        if self.product_id and self.warehouse_id and self.product.company_id != self.warehouse.company_id:
            raise ValidationError({"warehouse": "المخزن والصنف يجب أن يتبعا نفس الشركة."})
        if self.product_id and self.product.product_type != Product.ProductType.GOODS:
            raise ValidationError({"product": "حد إعادة الطلب يطبق على الأصناف المخزنية فقط."})


class StorageLocation(models.Model):
    """يمثل موقعًا فرعيًا داخل المخزن مثل الدور أو الرف أو الخانة."""
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
        if self.parent_id:
            visited = set()
            node = self.parent
            while node is not None:
                if self.pk and node.pk == self.pk:
                    raise ValidationError({"parent": "لا يمكن أن يكون الموقع تابعًا لأحد أبنائه."})
                if node.pk in visited:
                    raise ValidationError({"parent": "يوجد تسلسل أبوة دائري في مواقع التخزين."})
                visited.add(node.pk)
                node = node.parent



class StockMovement(models.Model):
    """يمثل مستند حركة مخزنية قبل الترحيل وبعده."""
    class MovementType(models.TextChoices):
        RECEIPT = "receipt", "استلام"
        ISSUE = "issue", "صرف"
        SUPPLIER_RETURN = "sup_return", "مرتجع مورد"
        SUPPLIER_RETURN_REVERSAL = "sup_rev", "عكس مرتجع مورد"
        SALES_RETURN_REVERSAL = "sal_rev", "عكس مردود مبيعات"
        TRANSFER = "transfer", "تحويل"
        ADJUSTMENT = "adjustment", "تسوية جرد"

    class AdjustmentReason(models.TextChoices):
        COUNT = "count", "جرد فعلي"
        DAMAGE = "damage", "تالف"
        EXPIRED = "expired", "منتهي الصلاحية"
        WRITE_OFF = "write_off", "شطب أو إعدام"
        OTHER = "other", "أخرى"

    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        POSTED = "posted", "مرحّل"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="stock_movements")
    movement_number = models.CharField(max_length=40)
    movement_type = models.CharField(max_length=12, choices=MovementType.choices)
    adjustment_reason = models.CharField(max_length=16, choices=AdjustmentReason.choices, blank=True)
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
            models.Index(fields=("status", "movement_date", "posted_at", "id"), name="inv_move_card_order_idx"),
        ]

    def clean(self):
        super().clean()
        for warehouse in (self.source_warehouse, self.destination_warehouse):
            if warehouse and warehouse.company_id != self.company_id:
                raise ValidationError("كل المخازن يجب أن تتبع نفس الشركة.")
        if self.movement_type == self.MovementType.RECEIPT and not self.destination_warehouse_id:
            raise ValidationError({"destination_warehouse": "حدد مخزن الاستلام."})
        if self.movement_type in (self.MovementType.ISSUE, self.MovementType.SUPPLIER_RETURN, self.MovementType.SALES_RETURN_REVERSAL) and not self.source_warehouse_id:
            raise ValidationError({"source_warehouse": "حدد مخزن الصرف."})
        if self.movement_type == self.MovementType.TRANSFER:
            if not self.source_warehouse_id or not self.destination_warehouse_id:
                raise ValidationError("التحويل يتطلب مخزن مصدر ومخزن وجهة.")
            if self.source_warehouse_id == self.destination_warehouse_id:
                raise ValidationError("لا يمكن التحويل إلى نفس المخزن.")
        if self.movement_type == self.MovementType.ADJUSTMENT and bool(self.source_warehouse_id) == bool(self.destination_warehouse_id):
            raise ValidationError("التسوية تتطلب تحديد مخزن واحد فقط.")
        if self.movement_type == self.MovementType.ADJUSTMENT and not self.adjustment_reason:
            raise ValidationError({"adjustment_reason": "حدد سبب التسوية."})
        if self.movement_type != self.MovementType.ADJUSTMENT and self.adjustment_reason:
            raise ValidationError({"adjustment_reason": "سبب التسوية لا يستخدم إلا مع حركات التسوية."})


class StockMovementLine(models.Model):
    """يحفظ صنفًا وكمية وتفاصيل التكلفة ضمن حركة مخزنية."""
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
    cost_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0, validators=[MinValueValidator(0)])
    adjustment_direction = models.CharField(max_length=3, choices=AdjustmentDirection.choices, blank=True)
    reversal_of_line = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversal_lines")

    class Meta:
        ordering = ("id",)
        indexes = [
            models.Index(fields=("product", "movement"), name="inv_line_prod_move_idx"),
        ]

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
    """يحفظ الرصيد الكمي والقيمة الدفترية للصنف في المخزن."""
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
    """يمثل طبقة تكلفة تستخدم في FIFO أو احتساب التكلفة."""
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="cost_layers")
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name="cost_layers")
    source_line = models.ForeignKey(StockMovementLine, on_delete=models.PROTECT, related_name="created_cost_layers")
    root_source_line = models.ForeignKey(StockMovementLine, null=True, blank=True, on_delete=models.PROTECT, related_name="root_cost_layers")
    parent_layer = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="transferred_layers")
    location = models.ForeignKey(StorageLocation, null=True, blank=True, on_delete=models.PROTECT, related_name="fifo_cost_layers")
    expiry_date = models.DateField(null=True, blank=True)
    quantity_remaining = models.DecimalField(max_digits=20, decimal_places=4, validators=[MinValueValidator(0)])
    unit_cost = models.DecimalField(max_digits=20, decimal_places=8, validators=[MinValueValidator(0)])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "id")
        indexes = [models.Index(fields=("product", "warehouse", "location", "created_at"), name="inv_fifo_layer_order_idx")]
        constraints = [models.CheckConstraint(condition=Q(quantity_remaining__gte=0), name="ck_fifo_layer_nonnegative")]


class StockCostConsumption(models.Model):
    """يسجل استهلاك كمية من طبقة تكلفة بعينها مع الاحتفاظ بمصدرها الأصلي."""
    movement_line = models.ForeignKey(StockMovementLine, on_delete=models.PROTECT, related_name="cost_consumptions")
    source_layer = models.ForeignKey(StockCostLayer, on_delete=models.PROTECT, related_name="consumptions")
    quantity = models.DecimalField(max_digits=20, decimal_places=4, validators=[MinValueValidator(0.0001)])
    unit_cost = models.DecimalField(max_digits=20, decimal_places=6, validators=[MinValueValidator(0)])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("id",)
        indexes = [
            models.Index(fields=("movement_line", "source_layer"), name="inv_cost_consume_idx"),
            models.Index(fields=("source_layer", "movement_line"), name="inv_consume_source_move_idx"),
        ]


class StockMovementJournalLink(models.Model):
    """يربط حركة المخزون بقيد اليومية الناتج عن ترحيلها."""
    movement = models.ForeignKey(StockMovement, on_delete=models.PROTECT, related_name="journal_links")
    journal_entry = models.ForeignKey("accounting.JournalEntry", on_delete=models.PROTECT, related_name="stock_movement_links")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("movement", "journal_entry"), name="uq_stock_movement_journal_link"),
        ]
        ordering = ("id",)


class InventoryCount(models.Model):
    """مستند جرد فعلي يثبت لقطة الرصيد قبل اعتماد فروق الجرد."""
    class Status(models.TextChoices):
        DRAFT = "draft", "قيد العد"
        POSTED = "posted", "مرحّل"
        CANCELLED = "cancelled", "ملغي"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="inventory_counts")
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name="inventory_counts")
    count_number = models.CharField(max_length=40)
    count_date = models.DateField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT,
        related_name="created_inventory_counts",
    )
    posted_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT,
        related_name="posted_inventory_counts",
    )
    posted_at = models.DateTimeField(null=True, blank=True)
    gain_movement = models.ForeignKey(
        StockMovement, null=True, blank=True, on_delete=models.PROTECT,
        related_name="gain_inventory_counts",
    )
    loss_movement = models.ForeignKey(
        StockMovement, null=True, blank=True, on_delete=models.PROTECT,
        related_name="loss_inventory_counts",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-count_date", "-id")
        constraints = [
            models.UniqueConstraint(fields=("company", "count_number"), name="uq_inventory_count_company_number"),
            models.UniqueConstraint(
                fields=("warehouse",), condition=Q(status="draft"),
                name="uq_inventory_count_open_warehouse",
            ),
        ]
        indexes = [
            models.Index(fields=("company", "status", "count_date"), name="inv_count_status_date_idx"),
        ]
        verbose_name = "مستند جرد مخزني"
        verbose_name_plural = "مستندات الجرد المخزني"

    def clean(self):
        super().clean()
        if self.warehouse_id and self.company_id != self.warehouse.company_id:
            raise ValidationError({"warehouse": "المخزن لا يتبع الشركة المحددة."})


class InventoryCountLine(models.Model):
    """سطر جرد يحتفظ برصيد الدفاتر وقت بدء الجرد والكمية الفعلية المعدودة."""
    count = models.ForeignKey(InventoryCount, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="inventory_count_lines")
    location = models.ForeignKey(
        StorageLocation, null=True, blank=True, on_delete=models.PROTECT,
        related_name="inventory_count_lines",
    )
    expiry_date = models.DateField(null=True, blank=True)
    system_quantity = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    system_value = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    counted_quantity = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    adjustment_unit_cost = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    notes = models.TextField(blank=True)
    counted_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT,
        related_name="counted_inventory_lines",
    )
    counted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def company_id(self):
        """Expose the owning company for the shared company-role permission."""
        return self.count.company_id

    class Meta:
        ordering = ("product__code", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("count", "product", "location", "expiry_date"),
                nulls_distinct=False,
                name="uq_inventory_count_line_dimension",
            ),
            models.CheckConstraint(condition=Q(system_quantity__gte=0), name="ck_inv_count_system_qty_nonnegative"),
            models.CheckConstraint(condition=Q(system_value__gte=0), name="ck_inv_count_system_value_nonnegative"),
            models.CheckConstraint(condition=Q(counted_quantity__isnull=True) | Q(counted_quantity__gte=0), name="ck_inv_count_counted_qty_nonnegative"),
            models.CheckConstraint(condition=Q(adjustment_unit_cost__gte=0), name="ck_inv_count_unit_cost_nonnegative"),
        ]
        indexes = [
            models.Index(fields=("count", "product"), name="inv_count_line_product_idx"),
        ]
        verbose_name = "بند جرد مخزني"
        verbose_name_plural = "بنود الجرد المخزني"

    @property
    def variance_quantity(self):
        if self.counted_quantity is None:
            return None
        return self.counted_quantity - self.system_quantity

    def clean(self):
        super().clean()
        if self.count_id and self.product_id:
            if self.product.company_id != self.count.company_id:
                raise ValidationError({"product": "الصنف لا يتبع شركة مستند الجرد."})
            if self.product.product_type != Product.ProductType.GOODS:
                raise ValidationError({"product": "لا يمكن جرد خدمة غير مخزنية."})
            if self.location_id and self.location.warehouse_id != self.count.warehouse_id:
                raise ValidationError({"location": "موقع الجرد لا يتبع مخزن المستند."})
            if self.product.track_expiry and not self.expiry_date:
                raise ValidationError({"expiry_date": "تاريخ الصلاحية مطلوب للصنف المتتبع."})
            if not self.product.track_expiry and self.expiry_date:
                raise ValidationError({"expiry_date": "الصنف لا يتطلب تتبع الصلاحية."})
            if self.counted_quantity is not None:
                quantum = Decimal("1").scaleb(-self.product.unit.decimal_places)
                if self.counted_quantity != self.counted_quantity.quantize(quantum):
                    raise ValidationError({"counted_quantity": "الكمية تتجاوز دقة وحدة قياس الصنف."})



class StockTransfer(models.Model):
    """مستند تحويل بين مخزنين يحتفظ بالكميات المشحونة والمستلمة والبضاعة بالطريق."""

    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        IN_TRANSIT = "in_transit", "بضاعة بالطريق"
        PARTIALLY_RECEIVED = "partially_received", "استلام جزئي"
        RECEIVED = "received", "مكتمل الاستلام"
        CANCELLED = "cancelled", "ملغي"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="stock_transfers")
    transfer_number = models.CharField(max_length=40)
    transfer_date = models.DateField()
    source_warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name="stock_transfers_out")
    destination_warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name="stock_transfers_in")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    dispatch_movement = models.OneToOneField(
        StockMovement, null=True, blank=True, on_delete=models.PROTECT,
        related_name="dispatched_stock_transfer",
    )
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT,
        related_name="created_stock_transfers",
    )
    dispatched_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT,
        related_name="dispatched_stock_transfers",
    )
    dispatched_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-transfer_date", "-id")
        constraints = [
            models.UniqueConstraint(fields=("company", "transfer_number"), name="uq_stock_transfer_company_number"),
        ]
        indexes = [
            models.Index(fields=("company", "status", "transfer_date"), name="inv_transfer_status_date_idx"),
        ]

    def clean(self):
        super().clean()
        if self.source_warehouse_id and self.destination_warehouse_id:
            if self.source_warehouse.company_id != self.company_id or self.destination_warehouse.company_id != self.company_id:
                raise ValidationError("مخزنا التحويل يجب أن يتبعا نفس الشركة.")
            if self.source_warehouse_id == self.destination_warehouse_id:
                raise ValidationError("لا يمكن التحويل إلى نفس المخزن.")
            if self.source_warehouse.is_transit or self.destination_warehouse.is_transit:
                raise ValidationError("لا يمكن اختيار مخزن البضاعة بالطريق كمخزن تشغيلي.")


class StockTransferLine(models.Model):
    """صنف وكمية التحويل مع رصيد الكمية التي لم تصل بعد."""

    transfer = models.ForeignKey(StockTransfer, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="stock_transfer_lines")
    expiry_date = models.DateField(null=True, blank=True)
    quantity = models.DecimalField(max_digits=18, decimal_places=4, validators=[MinValueValidator(0.0001)])
    received_quantity = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("product__code", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("transfer", "product", "expiry_date"),
                nulls_distinct=False,
                name="uq_stock_transfer_line_batch",
            ),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="ck_stock_transfer_qty_positive"),
            models.CheckConstraint(
                condition=Q(received_quantity__gte=0) & Q(received_quantity__lte=models.F("quantity")),
                name="ck_stock_transfer_received_range",
            ),
        ]

    @property
    def remaining_quantity(self):
        return self.quantity - self.received_quantity

    def clean(self):
        super().clean()
        if self.transfer_id and self.product_id:
            if self.product.company_id != self.transfer.company_id:
                raise ValidationError({"product": "الصنف لا يتبع شركة التحويل."})
            if self.product.product_type != Product.ProductType.GOODS:
                raise ValidationError({"product": "تحويل المخزون متاح للأصناف المخزنية فقط."})
            if self.product.track_expiry and not self.expiry_date:
                raise ValidationError({"expiry_date": "تاريخ الصلاحية مطلوب للصنف المتتبع."})
            if not self.product.track_expiry and self.expiry_date:
                raise ValidationError({"expiry_date": "الصنف لا يتطلب تتبع الصلاحية."})


class StockTransferReceipt(models.Model):
    """عملية استلام مستقلة تسمح بتعدد دفعات الاستلام للتحويل الواحد."""

    transfer = models.ForeignKey(StockTransfer, on_delete=models.PROTECT, related_name="receipts")
    receipt_number = models.CharField(max_length=40)
    receipt_date = models.DateField()
    movement = models.OneToOneField(
        StockMovement, on_delete=models.PROTECT, related_name="stock_transfer_receipt",
    )
    received_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT,
        related_name="stock_transfer_receipts",
    )
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("receipt_date", "id")
        constraints = [
            models.UniqueConstraint(fields=("transfer", "receipt_number"), name="uq_stock_transfer_receipt_number"),
        ]


class StockTransferReceiptLine(models.Model):
    """يربط الكمية المستلمة ببند التحويل وبند حركة الإدخال الناتجة."""

    receipt = models.ForeignKey(StockTransferReceipt, on_delete=models.CASCADE, related_name="lines")
    transfer_line = models.ForeignKey(StockTransferLine, on_delete=models.PROTECT, related_name="receipt_lines")
    movement_line = models.OneToOneField(
        StockMovementLine, on_delete=models.PROTECT, related_name="stock_transfer_receipt_line",
    )
    quantity = models.DecimalField(max_digits=18, decimal_places=4, validators=[MinValueValidator(0.0001)])

    class Meta:
        ordering = ("id",)
        constraints = [
            models.UniqueConstraint(fields=("receipt", "transfer_line"), name="uq_transfer_receipt_line"),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="ck_transfer_receipt_qty_positive"),
        ]


class WarehouseStatusAudit(models.Model):
    """Audit trail for warehouse activation and reopening."""
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name="status_audit_entries",
    )
    previous_is_active = models.BooleanField()
    new_is_active = models.BooleanField()
    reason = models.TextField(blank=True)
    changed_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT,
        related_name="warehouse_status_audits",
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-changed_at", "-pk")
        indexes = [models.Index(fields=("warehouse", "-changed_at"), name="wh_status_audit_idx")]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("سجل تدقيق حالة المخزن غير قابل للتعديل.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("لا يمكن حذف سجل تدقيق حالة المخزن.")
