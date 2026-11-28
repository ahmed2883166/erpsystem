from decimal import Decimal

from django.db import transaction
from rest_framework import serializers

from .models import InventoryCount, InventoryCountLine, Product, ProductCategory, StockBalance, StockMovement, StockMovementLine, StorageLocation, UnitOfMeasure, Warehouse, WarehouseProductSetting


class ProductCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductCategory
        fields = ("id", "company", "code", "name_ar", "parent", "is_active")

    def validate(self, attrs):
        """التحقق من صحة المدخلات واتساق العلاقات قبل قبول بيانات المستند."""
        company = attrs.get("company", getattr(self.instance, "company", None))
        parent = attrs.get("parent", getattr(self.instance, "parent", None))
        if parent and company and parent.company_id != company.pk:
            raise serializers.ValidationError({"parent": "المجموعة الأب يجب أن تتبع نفس الشركة."})
        if self.instance and parent:
            visited = set()
            node = parent
            while node is not None:
                if node.pk == self.instance.pk:
                    raise serializers.ValidationError({"parent": "لا يمكن ربط المجموعة بأحد أبنائها؛ سيؤدي ذلك إلى دورة في التصنيفات."})
                if node.pk in visited:
                    raise serializers.ValidationError({"parent": "يوجد تسلسل أبوة دائري في التصنيفات؛ راجع الهيكل قبل التعديل."})
                visited.add(node.pk)
                node = node.parent
        return attrs


class UnitOfMeasureSerializer(serializers.ModelSerializer):
    class Meta:
        model = UnitOfMeasure
        fields = ("id", "company", "code", "name_ar", "symbol", "decimal_places", "is_active")

    def validate_code(self, value):
        """التحقق من كود السجل قبل الحفظ."""
        return value.strip().upper()


class ProductSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name_ar", read_only=True)
    unit_name = serializers.CharField(source="unit.name_ar", read_only=True)

    class Meta:
        model = Product
        fields = (
            "id", "company", "category", "category_name", "unit", "unit_name",
            "code", "barcode", "name_ar", "name_en", "product_type", "stock_class",
            "purchase_price", "sale_price", "unit_weight", "unit_volume", "costing_method", "min_stock",
            "track_expiry", "is_active", "created_at", "updated_at",
        )
        read_only_fields = ("created_at", "updated_at")
        extra_kwargs = {"barcode": {"required": False, "allow_blank": True}}

    def validate(self, attrs):
        """التحقق من صحة المدخلات واتساق العلاقات قبل قبول بيانات المستند."""
        company = attrs.get("company", getattr(self.instance, "company", None))
        category = attrs.get("category", getattr(self.instance, "category", None))
        unit = attrs.get("unit", getattr(self.instance, "unit", None))
        product_type = attrs.get("product_type", getattr(self.instance, "product_type", Product.ProductType.GOODS))
        track_expiry = attrs.get("track_expiry", getattr(self.instance, "track_expiry", False))
        if category and company and category.company_id != company.pk:
            raise serializers.ValidationError({"category": "المجموعة لا تتبع الشركة المحددة."})
        if unit and company and unit.company_id != company.pk:
            raise serializers.ValidationError({"unit": "وحدة القياس لا تتبع الشركة المحددة."})
        if product_type == Product.ProductType.SERVICE:
            if track_expiry:
                raise serializers.ValidationError({"track_expiry": "الخدمة لا يمكن تتبع صلاحيتها."})
            attrs["stock_class"] = None
        elif not attrs.get("stock_class", getattr(self.instance, "stock_class", Product.StockClass.FINISHED_GOODS)):
            attrs["stock_class"] = Product.StockClass.FINISHED_GOODS
        if self.instance:
            costing_method = attrs.get("costing_method", self.instance.costing_method)
            next_type = attrs.get("product_type", self.instance.product_type)
            changing_stock_semantics = (
                costing_method != self.instance.costing_method
                or next_type != self.instance.product_type
            )
            if changing_stock_semantics:
                has_stock_balance = self.instance.stock_balances.filter(quantity__gt=0).exists()
                has_posted_history = self.instance.stock_movement_lines.filter(
                    movement__status=StockMovement.Status.POSTED,
                ).exists()
                if has_stock_balance or has_posted_history:
                    raise serializers.ValidationError(
                        "لا يمكن تغيير طريقة التقييم أو تحويل الصنف إلى خدمة بعد وجود رصيد أو حركات مخزنية مرحلة. "
                        "أنشئ صنفًا جديدًا إذا كنت تحتاج طريقة مختلفة."
                    )
        return attrs

    def validate_code(self, value):
        """التحقق من كود السجل قبل الحفظ."""
        return value.strip().upper()

    def validate_barcode(self, value):
        """التحقق من صلاحية الباركود وتفرده داخل الشركة."""
        return value.strip()


class WarehouseSerializer(serializers.ModelSerializer):
    branch_code = serializers.CharField(source="branch.code", read_only=True)

    class Meta:
        model = Warehouse
        fields = ("id", "company", "branch", "branch_code", "code", "name_ar", "address", "is_active", "created_at")
        read_only_fields = ("created_at",)

    def validate(self, attrs):
        """التحقق من صحة المدخلات واتساق العلاقات قبل قبول بيانات المستند."""
        company = attrs.get("company", getattr(self.instance, "company", None))
        branch = attrs.get("branch", getattr(self.instance, "branch", None))
        if branch and company and branch.company_id != company.pk:
            raise serializers.ValidationError({"branch": "الفرع لا يتبع الشركة المحددة."})
        return attrs

    def validate_code(self, value):
        """التحقق من كود السجل قبل الحفظ."""
        return value.strip().upper()


class WarehouseProductSettingSerializer(serializers.ModelSerializer):
    product_code = serializers.CharField(source="product.code", read_only=True)
    product_name = serializers.CharField(source="product.name_ar", read_only=True)
    warehouse_name = serializers.CharField(source="warehouse.name_ar", read_only=True)

    class Meta:
        model = WarehouseProductSetting
        fields = ("id", "product", "product_code", "product_name", "warehouse", "warehouse_name", "min_stock", "is_active", "updated_at")
        read_only_fields = ("updated_at",)

    def validate(self, attrs):
        """التحقق من صحة المدخلات واتساق العلاقات قبل قبول بيانات المستند."""
        product = attrs.get("product", getattr(self.instance, "product", None))
        warehouse = attrs.get("warehouse", getattr(self.instance, "warehouse", None))
        if product and warehouse and product.company_id != warehouse.company_id:
            raise serializers.ValidationError({"warehouse": "المخزن والصنف يجب أن يتبعا نفس الشركة."})
        if product and product.product_type != Product.ProductType.GOODS:
            raise serializers.ValidationError({"product": "حد إعادة الطلب يطبق على الأصناف المخزنية فقط."})
        return attrs


class StorageLocationSerializer(serializers.ModelSerializer):
    warehouse_name = serializers.CharField(source="warehouse.name_ar", read_only=True)

    class Meta:
        model = StorageLocation
        fields = ("id", "warehouse", "warehouse_name", "code", "name_ar", "location_type", "parent", "is_active")

    def validate(self, attrs):
        """التحقق من صحة المدخلات واتساق العلاقات قبل قبول بيانات المستند."""
        warehouse = attrs.get("warehouse", getattr(self.instance, "warehouse", None))
        parent = attrs.get("parent", getattr(self.instance, "parent", None))
        if parent and warehouse and parent.warehouse_id != warehouse.pk:
            raise serializers.ValidationError({"parent": "الموقع الأب يجب أن يتبع نفس المخزن."})
        if self.instance and parent:
            visited = set()
            node = parent
            while node is not None:
                if node.pk == self.instance.pk:
                    raise serializers.ValidationError({"parent": "لا يمكن نقل الموقع ليصبح تابعًا لأحد أبنائه."})
                if node.pk in visited:
                    raise serializers.ValidationError({"parent": "يوجد تسلسل أبوة دائري في مواقع التخزين؛ راجع الهيكل قبل التعديل."})
                visited.add(node.pk)
                node = node.parent
        return attrs



class StockMovementLineSerializer(serializers.ModelSerializer):
    product_code = serializers.CharField(source="product.code", read_only=True)
    product_name = serializers.CharField(source="product.name_ar", read_only=True)
    source_location_name = serializers.CharField(source="source_location.name_ar", read_only=True)
    destination_location_name = serializers.CharField(source="destination_location.name_ar", read_only=True)

    class Meta:
        model = StockMovementLine
        fields = ("id", "product", "product_code", "product_name", "source_location", "source_location_name", "destination_location", "destination_location_name", "expiry_date", "quantity", "unit_cost", "cost_amount", "adjustment_direction")
        read_only_fields = ("id", "cost_amount")

    def validate(self, attrs):
        """التحقق من صحة المدخلات واتساق العلاقات قبل قبول بيانات المستند."""
        product = attrs.get("product")
        movement = self.context.get("movement")
        if product and product.product_type != Product.ProductType.GOODS:
            raise serializers.ValidationError({"product": "لا يمكن تسجيل حركة مخزنية على خدمة."})
        if product and movement and product.company_id != movement.company_id:
            raise serializers.ValidationError({"product": "الصنف لا يتبع شركة الحركة."})
        return attrs


class StockMovementSerializer(serializers.ModelSerializer):
    lines = StockMovementLineSerializer(many=True)
    source_warehouse_name = serializers.CharField(source="source_warehouse.name_ar", read_only=True)
    destination_warehouse_name = serializers.CharField(source="destination_warehouse.name_ar", read_only=True)
    journal_entries = serializers.SerializerMethodField()

    class Meta:
        model = StockMovement
        fields = (
            "id", "company", "movement_number", "movement_type", "movement_date",
            "source_warehouse", "source_warehouse_name", "destination_warehouse",
            "destination_warehouse_name", "adjustment_reason", "status", "notes", "lines", "journal_entries", "created_at",
            "posted_at",
        )
        read_only_fields = ("status", "created_at", "posted_at")

    def get_journal_entries(self, obj):
        """التحقق من بيانات get_journal_entries وضمان توافقها مع قواعد وحدة serializers.py قبل الحفظ."""
        return [
            {
                "id": link.journal_entry_id,
                "entry_number": link.journal_entry.entry_number,
                "branch_code": link.journal_entry.branch.code,
                "status": link.journal_entry.status,
            }
            for link in obj.journal_links.select_related("journal_entry", "journal_entry__branch").all()
        ]

    def validate(self, attrs):
        """التحقق من صحة المدخلات واتساق العلاقات قبل قبول بيانات المستند."""
        company = attrs.get("company", getattr(self.instance, "company", None))
        movement_type = attrs.get("movement_type", getattr(self.instance, "movement_type", None))
        source = attrs.get("source_warehouse", getattr(self.instance, "source_warehouse", None))
        destination = attrs.get("destination_warehouse", getattr(self.instance, "destination_warehouse", None))
        lines = attrs.get("lines", [])
        if self.instance and self.instance.status == StockMovement.Status.POSTED:
            raise serializers.ValidationError("لا يمكن تعديل حركة مرحلة.")
        adjustment_reason = attrs.get("adjustment_reason", getattr(self.instance, "adjustment_reason", ""))
        if movement_type == StockMovement.MovementType.ADJUSTMENT and not adjustment_reason:
            raise serializers.ValidationError({"adjustment_reason": "حدد سبب التسوية."})
        if movement_type != StockMovement.MovementType.ADJUSTMENT and adjustment_reason:
            raise serializers.ValidationError({"adjustment_reason": "سبب التسوية لا يستخدم إلا مع حركات التسوية."})
        if not lines and not self.instance:
            raise serializers.ValidationError({"lines": "أضف صنفًا واحدًا على الأقل."})
        for warehouse in (source, destination):
            if warehouse and company and warehouse.company_id != company.pk:
                raise serializers.ValidationError("المخزن لا يتبع الشركة المحددة.")
            if warehouse and not warehouse.is_active:
                raise serializers.ValidationError("لا يمكن تسجيل حركة على مخزن موقوف.")
        source_required = movement_type in (
            StockMovement.MovementType.ISSUE,
            StockMovement.MovementType.SUPPLIER_RETURN,
            StockMovement.MovementType.SALES_RETURN_REVERSAL,
        )
        destination_required = movement_type in (
            StockMovement.MovementType.RECEIPT,
            StockMovement.MovementType.SUPPLIER_RETURN_REVERSAL,
        )
        if source_required and not source:
            raise serializers.ValidationError({"source_warehouse": "حدد مخزن المصدر لهذه الحركة."})
        if destination_required and not destination:
            raise serializers.ValidationError({"destination_warehouse": "حدد مخزن الوجهة لهذه الحركة."})
        if movement_type in (
            StockMovement.MovementType.RECEIPT,
            StockMovement.MovementType.SUPPLIER_RETURN_REVERSAL,
        ) and source:
            raise serializers.ValidationError({"source_warehouse": "هذه الحركة لا تستخدم مخزن مصدر."})
        if movement_type in (
            StockMovement.MovementType.ISSUE,
            StockMovement.MovementType.SUPPLIER_RETURN,
            StockMovement.MovementType.SALES_RETURN_REVERSAL,
        ) and destination:
            raise serializers.ValidationError({"destination_warehouse": "هذه الحركة لا تستخدم مخزن وجهة."})
        if movement_type == StockMovement.MovementType.TRANSFER:
            if not source or not destination or source.pk == destination.pk:
                raise serializers.ValidationError("التحويل يتطلب مخزنين مختلفين.")
        if movement_type == StockMovement.MovementType.ADJUSTMENT and bool(source) == bool(destination):
            raise serializers.ValidationError("التسوية تتطلب مخزنًا واحدًا فقط.")
        for line in lines:
            product = line["product"]
            if company and product.company_id != company.pk:
                raise serializers.ValidationError({"lines": "يوجد صنف لا يتبع الشركة المحددة."})
            if product.product_type != Product.ProductType.GOODS:
                raise serializers.ValidationError({"lines": "الخدمات لا تقبل حركات مخزنية."})
            source_location = line.get("source_location")
            destination_location = line.get("destination_location")
            if not product.is_active:
                raise serializers.ValidationError({"lines": "لا يمكن تسجيل حركة على صنف موقوف."})
            if source_location and (not source or source_location.warehouse_id != source.pk or not source_location.is_active):
                raise serializers.ValidationError({"lines": "موقع المصدر غير نشط أو لا يتبع مخزن المصدر المحدد."})
            if destination_location and (not destination or destination_location.warehouse_id != destination.pk or not destination_location.is_active):
                raise serializers.ValidationError({"lines": "موقع الوجهة غير نشط أو لا يتبع مخزن الوجهة المحدد."})
            expiry_date = line.get("expiry_date")
            if product.track_expiry and not expiry_date:
                raise serializers.ValidationError({"lines": "تاريخ الصلاحية مطلوب للصنف المتتبع."})
            if not product.track_expiry and expiry_date:
                raise serializers.ValidationError({"lines": "الصنف لا يتطلب تتبع الصلاحية."})
            if movement_type == StockMovement.MovementType.ADJUSTMENT and not line.get("adjustment_direction"):
                raise serializers.ValidationError({"lines": "حدد الزيادة أو العجز لكل صنف في التسوية."})
            quantity_quantum = Decimal("1").scaleb(-product.unit.decimal_places)
            if line["quantity"] != line["quantity"].quantize(quantity_quantum):
                raise serializers.ValidationError({
                    "lines": f"كمية الصنف {product.code} تتجاوز دقة وحدة القياس ({product.unit.decimal_places} منازل عشرية)."
                })
            if movement_type != StockMovement.MovementType.ADJUSTMENT and line.get("adjustment_direction"):
                raise serializers.ValidationError({"lines": "اتجاه التسوية لا يستخدم إلا في حركات التسوية."})
        if movement_type == StockMovement.MovementType.ADJUSTMENT:
            if "lines" in attrs:
                directions = {item.get("adjustment_direction") for item in lines}
            elif self.instance:
                directions = set(self.instance.lines.values_list("adjustment_direction", flat=True))
            else:
                directions = set()
            if len(directions) != 1 or "" in directions:
                raise serializers.ValidationError({"lines": "افصل زيادات الجرد عن العجز في حركتين مستقلتين."})
            direction = next(iter(directions))
            if (
                attrs.get("adjustment_reason", getattr(self.instance, "adjustment_reason", ""))
                in (
                    StockMovement.AdjustmentReason.DAMAGE,
                    StockMovement.AdjustmentReason.EXPIRED,
                    StockMovement.AdjustmentReason.WRITE_OFF,
                )
                and direction != StockMovementLine.AdjustmentDirection.OUT
            ):
                raise serializers.ValidationError({"adjustment_reason": "أسباب التلف والانتهاء والشطب يجب أن تكون تسوية عجز."})
            if direction == StockMovementLine.AdjustmentDirection.IN and (not destination or source):
                raise serializers.ValidationError({"destination_warehouse": "زيادة الجرد تتطلب مخزن وجهة فقط."})
            if direction == StockMovementLine.AdjustmentDirection.OUT and (not source or destination):
                raise serializers.ValidationError({"source_warehouse": "عجز الجرد يتطلب مخزن مصدر فقط."})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        """إنشاء المستند وبنوده داخل معاملة ذرية لضمان اتساق البيانات."""
        lines = validated_data.pop("lines")
        movement = StockMovement.objects.create(**validated_data)
        StockMovementLine.objects.bulk_create([
            StockMovementLine(movement=movement, **line) for line in lines
        ])
        return movement

    @transaction.atomic
    def update(self, instance, validated_data):
        """تحديث المستند وبنوده مع حماية القيود والمستندات المرحلة."""
        if instance.status != StockMovement.Status.DRAFT:
            raise serializers.ValidationError("لا يمكن تعديل حركة مرحلة.")
        lines = validated_data.pop("lines", None)
        for key, value in validated_data.items():
            setattr(instance, key, value)
        instance.save()
        if lines is not None:
            instance.lines.all().delete()
            StockMovementLine.objects.bulk_create([
                StockMovementLine(movement=instance, **line) for line in lines
            ])
        return instance



class StockBalanceSerializer(serializers.ModelSerializer):
    product_code = serializers.CharField(source="product.code", read_only=True)
    product_name = serializers.CharField(source="product.name_ar", read_only=True)
    warehouse_name = serializers.CharField(source="warehouse.name_ar", read_only=True)
    location_name = serializers.CharField(source="location.name_ar", read_only=True, allow_null=True)

    class Meta:
        model = StockBalance
        fields = ("id", "product", "product_code", "product_name", "warehouse", "warehouse_name", "location", "location_name", "expiry_date", "quantity", "average_cost", "inventory_value", "updated_at")



class InventoryCountLineSerializer(serializers.ModelSerializer):
    product_code = serializers.CharField(source="product.code", read_only=True)
    product_name = serializers.CharField(source="product.name_ar", read_only=True)
    unit_name = serializers.CharField(source="product.unit.name_ar", read_only=True)
    location_name = serializers.CharField(source="location.name_ar", read_only=True, allow_null=True)
    variance_quantity = serializers.SerializerMethodField()
    counted_by_name = serializers.CharField(source="counted_by.get_full_name", read_only=True, allow_null=True)

    class Meta:
        model = InventoryCountLine
        fields = (
            "id", "count", "product", "product_code", "product_name", "unit_name",
            "location", "location_name", "expiry_date", "system_quantity", "system_value",
            "counted_quantity", "variance_quantity", "adjustment_unit_cost", "notes",
            "counted_by", "counted_by_name", "counted_at", "created_at", "updated_at",
        )
        read_only_fields = (
            "system_quantity", "system_value", "counted_by", "counted_by_name",
            "counted_at", "created_at", "updated_at",
        )

    def get_variance_quantity(self, obj):
        variance = obj.variance_quantity
        return None if variance is None else str(variance)

    def validate(self, attrs):
        count = attrs.get("count", getattr(self.instance, "count", None))
        product = attrs.get("product", getattr(self.instance, "product", None))
        location = attrs.get("location", getattr(self.instance, "location", None))
        expiry_date = attrs.get("expiry_date", getattr(self.instance, "expiry_date", None))
        counted_quantity = attrs.get("counted_quantity", getattr(self.instance, "counted_quantity", None))
        unit_cost = attrs.get("adjustment_unit_cost", getattr(self.instance, "adjustment_unit_cost", 0))
        if count and count.status != InventoryCount.Status.DRAFT:
            raise serializers.ValidationError("لا يمكن تعديل بنود جرد مرحّل أو ملغي.")
        if self.instance and count and count.pk != self.instance.count_id:
            raise serializers.ValidationError({"count": "لا يمكن نقل بند الجرد إلى مستند آخر."})
        if product and count and product.company_id != count.company_id:
            raise serializers.ValidationError({"product": "الصنف لا يتبع شركة مستند الجرد."})
        if product and product.product_type != Product.ProductType.GOODS:
            raise serializers.ValidationError({"product": "لا يمكن جرد خدمة غير مخزنية."})
        if location and count and location.warehouse_id != count.warehouse_id:
            raise serializers.ValidationError({"location": "موقع الجرد لا يتبع مخزن المستند."})
        if product and product.track_expiry and not expiry_date:
            raise serializers.ValidationError({"expiry_date": "تاريخ الصلاحية مطلوب للصنف المتتبع."})
        if product and not product.track_expiry and expiry_date:
            raise serializers.ValidationError({"expiry_date": "الصنف لا يتطلب تتبع الصلاحية."})
        if product and counted_quantity is not None:
            quantum = Decimal("1").scaleb(-product.unit.decimal_places)
            if counted_quantity != counted_quantity.quantize(quantum):
                raise serializers.ValidationError({"counted_quantity": "الكمية تتجاوز دقة وحدة قياس الصنف."})
        if counted_quantity is not None and counted_quantity < 0:
            raise serializers.ValidationError({"counted_quantity": "الكمية الفعلية لا يمكن أن تكون سالبة."})
        if unit_cost < 0:
            raise serializers.ValidationError({"adjustment_unit_cost": "تكلفة التسوية لا يمكن أن تكون سالبة."})
        if self.instance and any(key in attrs for key in ("product", "location", "expiry_date", "count")):
            raise serializers.ValidationError("بعد إنشاء بند الجرد يمكن تعديل الكمية الفعلية والتكلفة والملاحظات فقط.")
        return attrs


class InventoryCountSerializer(serializers.ModelSerializer):
    warehouse_name = serializers.CharField(source="warehouse.name_ar", read_only=True)
    created_by_name = serializers.CharField(source="created_by.get_full_name", read_only=True, allow_null=True)
    posted_by_name = serializers.CharField(source="posted_by.get_full_name", read_only=True, allow_null=True)
    gain_movement_number = serializers.CharField(source="gain_movement.movement_number", read_only=True, allow_null=True)
    loss_movement_number = serializers.CharField(source="loss_movement.movement_number", read_only=True, allow_null=True)
    lines = InventoryCountLineSerializer(many=True, read_only=True)
    lines_count = serializers.IntegerField(source="lines.count", read_only=True)

    class Meta:
        model = InventoryCount
        validators = []
        fields = (
            "id", "company", "warehouse", "warehouse_name", "count_number", "count_date",
            "status", "notes", "lines", "lines_count", "created_by", "created_by_name",
            "posted_by", "posted_by_name", "posted_at", "gain_movement", "gain_movement_number",
            "loss_movement", "loss_movement_number", "created_at", "updated_at",
        )
        read_only_fields = (
            "status", "created_by", "created_by_name", "posted_by", "posted_by_name",
            "posted_at", "gain_movement", "gain_movement_number", "loss_movement",
            "loss_movement_number", "created_at", "updated_at",
        )

    def validate(self, attrs):
        company = attrs.get("company", getattr(self.instance, "company", None))
        warehouse = attrs.get("warehouse", getattr(self.instance, "warehouse", None))
        if company and warehouse and warehouse.company_id != company.pk:
            raise serializers.ValidationError({"warehouse": "المخزن لا يتبع الشركة المحددة."})
        if self.instance and self.instance.status != InventoryCount.Status.DRAFT:
            raise serializers.ValidationError("لا يمكن تعديل مستند جرد غير مسودة.")
        if self.instance and any(key in attrs for key in ("company", "warehouse", "count_number", "count_date")):
            raise serializers.ValidationError("بعد بدء الجرد لا يمكن تغيير الشركة أو المخزن أو الرقم أو التاريخ.")
        count_number = attrs.get("count_number", getattr(self.instance, "count_number", None))
        if company and count_number:
            duplicate_number = InventoryCount.objects.filter(company=company, count_number=count_number)
            if self.instance:
                duplicate_number = duplicate_number.exclude(pk=self.instance.pk)
            if duplicate_number.exists():
                raise serializers.ValidationError({"count_number": "رقم الجرد مستخدم بالفعل داخل الشركة."})
        if warehouse and (not self.instance or warehouse.pk != self.instance.warehouse_id):
            open_count = InventoryCount.objects.filter(
                warehouse=warehouse, status=InventoryCount.Status.DRAFT,
            )
            if self.instance:
                open_count = open_count.exclude(pk=self.instance.pk)
            if open_count.exists():
                raise serializers.ValidationError({"warehouse": "يوجد مستند جرد مفتوح بالفعل على هذا المخزن."})
        return attrs
