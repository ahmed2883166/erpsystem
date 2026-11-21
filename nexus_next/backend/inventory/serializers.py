from rest_framework import serializers

from .models import Product, ProductCategory, StorageLocation, UnitOfMeasure, Warehouse


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
