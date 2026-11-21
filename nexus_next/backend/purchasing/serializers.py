from decimal import Decimal
from django.db import transaction

from rest_framework import serializers

from inventory.models import Product, StockMovement, StockMovementLine, Warehouse, StorageLocation
from suppliers.models import Supplier
from .models import PurchaseInvoice, PurchaseInvoiceLine, PurchaseReceipt


class ReceiptLineInputSerializer(serializers.Serializer):
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())
    quantity = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0.0001"))
    unit_cost = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0"))
    destination_location = serializers.IntegerField(required=False, allow_null=True)
    expiry_date = serializers.DateField(required=False, allow_null=True)

    def validate(self, attrs):
        product = attrs["product"]
        if product.product_type != Product.ProductType.GOODS or not product.is_active:
            raise serializers.ValidationError("اختر صنفًا مخزنيًا نشطًا.")
        if product.track_expiry and not attrs.get("expiry_date"):
            raise serializers.ValidationError({"expiry_date": "تاريخ الصلاحية مطلوب لهذا الصنف."})
        if not product.track_expiry and attrs.get("expiry_date"):
            raise serializers.ValidationError({"expiry_date": "الصنف لا يتطلب تتبع الصلاحية."})
        return attrs



class PurchaseReceiptSerializer(serializers.ModelSerializer):
    lines = ReceiptLineInputSerializer(many=True, write_only=True)
    movement_status = serializers.CharField(source="stock_movement.status", read_only=True)
    movement_number = serializers.CharField(source="stock_movement.movement_number", read_only=True)
    supplier_name = serializers.CharField(source="supplier.name_ar", read_only=True)
    warehouse_name = serializers.CharField(source="warehouse.name_ar", read_only=True)
    line_items = serializers.SerializerMethodField()

    class Meta:
        model = PurchaseReceipt
        fields = (
            "id", "company", "supplier", "supplier_name", "warehouse", "warehouse_name",
            "receipt_number", "receipt_date", "supplier_delivery_ref", "notes",
            "movement_status", "movement_number", "line_items", "lines", "created_at",
        )
        read_only_fields = ("created_at", "movement_status", "movement_number", "line_items")

    def get_line_items(self, obj):
        return [{
            "id": line.pk, "product": line.product_id, "product_code": line.product.code,
            "product_name": line.product.name_ar, "quantity": str(line.quantity),
            "unit_cost": str(line.unit_cost), "cost_amount": str(line.cost_amount),
            "expiry_date": line.expiry_date.isoformat() if line.expiry_date else None,
        } for line in obj.stock_movement.lines.select_related("product").all()]

    def validate(self, attrs):
        company = attrs.get("company", getattr(self.instance, "company", None))
        supplier = attrs.get("supplier", getattr(self.instance, "supplier", None))
        warehouse = attrs.get("warehouse", getattr(self.instance, "warehouse", None))
        if supplier and company and supplier.company_id != company.pk:
            raise serializers.ValidationError({"supplier": "المورد لا يتبع الشركة المحددة."})
        if warehouse and company and warehouse.company_id != company.pk:
            raise serializers.ValidationError({"warehouse": "المخزن لا يتبع الشركة المحددة."})
        if warehouse and not warehouse.is_active:
            raise serializers.ValidationError({"warehouse": "مخزن الاستلام موقوف."})
        if self.instance:
            raise serializers.ValidationError("إذن الاستلام لا يمكن تعديله؛ أنشئ إذنًا جديدًا.")
        lines = attrs.get("lines", [])
        if not lines:
            raise serializers.ValidationError({"lines": "أضف صنفًا واحدًا على الأقل."})
        for line in lines:
            if company and line["product"].company_id != company.pk:
                raise serializers.ValidationError({"lines": "كل الأصناف يجب أن تتبع نفس الشركة."})
            location_id = line.get("destination_location")
            if location_id:
                location = StorageLocation.objects.filter(pk=location_id, warehouse=warehouse, is_active=True).first()
                if not location:
                    raise serializers.ValidationError({"lines": "موقع التخزين غير نشط أو لا يتبع مخزن الاستلام."})
                line["destination_location_obj"] = location
        return attrs

