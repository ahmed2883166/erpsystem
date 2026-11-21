from django.db import transaction
from rest_framework import serializers

from .models import Product, ProductCategory, StockMovement, StockMovementLine, StorageLocation, UnitOfMeasure, Warehouse


class ProductCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductCategory
        fields = ("id", "company", "code", "name_ar", "parent", "is_active")

    def validate(self, attrs):
        company = attrs.get("company", getattr(self.instance, "company", None))
        parent = attrs.get("parent", getattr(self.instance, "parent", None))
        if parent and company and parent.company_id != company.pk:
            raise serializers.ValidationError({"parent": "المجموعة الأب يجب أن تتبع نفس الشركة."})
        if self.instance and parent and parent.pk == self.instance.pk:
            raise serializers.ValidationError({"parent": "لا يمكن ربط المجموعة بنفسها."})
        return attrs


class UnitOfMeasureSerializer(serializers.ModelSerializer):
    class Meta:
        model = UnitOfMeasure
        fields = ("id", "company", "code", "name_ar", "symbol", "decimal_places", "is_active")

    def validate_code(self, value):
        return value.strip().upper()


class ProductSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name_ar", read_only=True)
    unit_name = serializers.CharField(source="unit.name_ar", read_only=True)

    class Meta:
        model = Product
        fields = (
            "id", "company", "category", "category_name", "unit", "unit_name",
            "code", "barcode", "name_ar", "name_en", "product_type",
            "purchase_price", "sale_price", "costing_method", "min_stock",
            "track_expiry", "is_active", "created_at", "updated_at",
        )
        read_only_fields = ("created_at", "updated_at")
        extra_kwargs = {"barcode": {"required": False, "allow_blank": True}}

    def validate(self, attrs):
        company = attrs.get("company", getattr(self.instance, "company", None))
        category = attrs.get("category", getattr(self.instance, "category", None))
        unit = attrs.get("unit", getattr(self.instance, "unit", None))
        product_type = attrs.get("product_type", getattr(self.instance, "product_type", Product.ProductType.GOODS))
        track_expiry = attrs.get("track_expiry", getattr(self.instance, "track_expiry", False))
        if category and company and category.company_id != company.pk:
            raise serializers.ValidationError({"category": "المجموعة لا تتبع الشركة المحددة."})
        if unit and company and unit.company_id != company.pk:
            raise serializers.ValidationError({"unit": "وحدة القياس لا تتبع الشركة المحددة."})
        if product_type == Product.ProductType.SERVICE and track_expiry:
            raise serializers.ValidationError({"track_expiry": "الخدمة لا يمكن تتبع صلاحيتها."})
        return attrs

    def validate_code(self, value):
        return value.strip().upper()

    def validate_barcode(self, value):
        return value.strip()


class WarehouseSerializer(serializers.ModelSerializer):
    branch_code = serializers.CharField(source="branch.code", read_only=True)

    class Meta:
        model = Warehouse
        fields = ("id", "company", "branch", "branch_code", "code", "name_ar", "address", "is_active", "created_at")
        read_only_fields = ("created_at",)

    def validate(self, attrs):
        company = attrs.get("company", getattr(self.instance, "company", None))
        branch = attrs.get("branch", getattr(self.instance, "branch", None))
        if branch and company and branch.company_id != company.pk:
            raise serializers.ValidationError({"branch": "الفرع لا يتبع الشركة المحددة."})
        return attrs

    def validate_code(self, value):
        return value.strip().upper()


class StorageLocationSerializer(serializers.ModelSerializer):
    warehouse_name = serializers.CharField(source="warehouse.name_ar", read_only=True)

    class Meta:
        model = StorageLocation
        fields = ("id", "warehouse", "warehouse_name", "code", "name_ar", "location_type", "parent", "is_active")

    def validate(self, attrs):
        warehouse = attrs.get("warehouse", getattr(self.instance, "warehouse", None))
        parent = attrs.get("parent", getattr(self.instance, "parent", None))
        if parent and warehouse and parent.warehouse_id != warehouse.pk:
            raise serializers.ValidationError({"parent": "الموقع الأب يجب أن يتبع نفس المخزن."})
        if self.instance and parent and parent.pk == self.instance.pk:
            raise serializers.ValidationError({"parent": "لا يمكن ربط الموقع بنفسه."})
        return attrs



class StockMovementLineSerializer(serializers.ModelSerializer):
    product_code = serializers.CharField(source="product.code", read_only=True)
    product_name = serializers.CharField(source="product.name_ar", read_only=True)

    class Meta:
        model = StockMovementLine
        fields = ("id", "product", "product_code", "product_name", "quantity", "unit_cost", "adjustment_direction")
        read_only_fields = ("id",)

    def validate(self, attrs):
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

    class Meta:
        model = StockMovement
        fields = (
            "id", "company", "movement_number", "movement_type", "movement_date",
            "source_warehouse", "source_warehouse_name", "destination_warehouse",
            "destination_warehouse_name", "status", "notes", "lines", "created_at",
            "posted_at",
        )
        read_only_fields = ("status", "created_at", "posted_at")

    def validate(self, attrs):
        company = attrs.get("company", getattr(self.instance, "company", None))
        movement_type = attrs.get("movement_type", getattr(self.instance, "movement_type", None))
        source = attrs.get("source_warehouse", getattr(self.instance, "source_warehouse", None))
        destination = attrs.get("destination_warehouse", getattr(self.instance, "destination_warehouse", None))
        lines = attrs.get("lines", [])
        if self.instance and self.instance.status == StockMovement.Status.POSTED:
            raise serializers.ValidationError("لا يمكن تعديل حركة مرحلة.")
        if not lines and not self.instance:
            raise serializers.ValidationError({"lines": "أضف صنفًا واحدًا على الأقل."})
        for warehouse in (source, destination):
            if warehouse and company and warehouse.company_id != company.pk:
                raise serializers.ValidationError("المخزن لا يتبع الشركة المحددة.")
        if movement_type == StockMovement.MovementType.RECEIPT and not destination:
            raise serializers.ValidationError({"destination_warehouse": "حدد مخزن الاستلام."})
        if movement_type == StockMovement.MovementType.ISSUE and not source:
            raise serializers.ValidationError({"source_warehouse": "حدد مخزن الصرف."})
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
            if movement_type == StockMovement.MovementType.ADJUSTMENT and not line.get("adjustment_direction"):
                raise serializers.ValidationError({"lines": "حدد الزيادة أو العجز لكل صنف في التسوية."})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        lines = validated_data.pop("lines")
        movement = StockMovement.objects.create(**validated_data)
        StockMovementLine.objects.bulk_create([
            StockMovementLine(movement=movement, **line) for line in lines
        ])
        return movement
