from decimal import Decimal

from django.db import transaction
from rest_framework import serializers

from .models import (
    Product, StockTransfer, StockTransferLine, StockTransferReceipt,
    StockTransferReceiptLine,
)


class StockTransferLineSerializer(serializers.ModelSerializer):
    product_code = serializers.CharField(source="product.code", read_only=True)
    product_name = serializers.CharField(source="product.name_ar", read_only=True)
    received_quantity = serializers.DecimalField(max_digits=18, decimal_places=4, read_only=True)
    remaining_quantity = serializers.SerializerMethodField()

    class Meta:
        model = StockTransferLine
        fields = (
            "id", "product", "product_code", "product_name", "expiry_date",
            "quantity", "received_quantity", "remaining_quantity",
        )
        read_only_fields = ("id", "received_quantity", "remaining_quantity")

    def get_remaining_quantity(self, obj):
        return str(obj.remaining_quantity)


class StockTransferSerializer(serializers.ModelSerializer):
    lines = StockTransferLineSerializer(many=True)
    source_warehouse_name = serializers.CharField(source="source_warehouse.name_ar", read_only=True)
    destination_warehouse_name = serializers.CharField(source="destination_warehouse.name_ar", read_only=True)
    created_by_name = serializers.CharField(source="created_by.get_full_name", read_only=True, allow_null=True)
    dispatched_by_name = serializers.CharField(source="dispatched_by.get_full_name", read_only=True, allow_null=True)
    receipts_count = serializers.IntegerField(source="receipts.count", read_only=True)

    class Meta:
        model = StockTransfer
        fields = (
            "id", "company", "transfer_number", "transfer_date", "source_warehouse",
            "source_warehouse_name", "destination_warehouse", "destination_warehouse_name",
            "status", "notes", "lines", "dispatch_movement", "created_by", "created_by_name",
            "dispatched_by", "dispatched_by_name", "dispatched_at", "completed_at",
            "receipts_count", "created_at", "updated_at",
        )
        read_only_fields = (
            "status", "dispatch_movement", "created_by", "created_by_name", "dispatched_by",
            "dispatched_by_name", "dispatched_at", "completed_at", "receipts_count",
            "created_at", "updated_at",
        )

    def validate(self, attrs):
        company = attrs.get("company", getattr(self.instance, "company", None))
        source = attrs.get("source_warehouse", getattr(self.instance, "source_warehouse", None))
        destination = attrs.get("destination_warehouse", getattr(self.instance, "destination_warehouse", None))
        lines = attrs.get("lines")
        if self.instance and self.instance.status != StockTransfer.Status.DRAFT:
            raise serializers.ValidationError("لا يمكن تعديل تحويل تم شحنه أو استلامه.")
        if source and destination:
            if source.company_id != destination.company_id or (company and source.company_id != company.pk):
                raise serializers.ValidationError("مخزنا التحويل يجب أن يتبعا نفس الشركة.")
            if source.pk == destination.pk:
                raise serializers.ValidationError("لا يمكن التحويل إلى نفس المخزن.")
            if source.is_transit or destination.is_transit:
                raise serializers.ValidationError("مخزن البضاعة بالطريق لا يختار كمخزن تشغيلي.")
            if not source.is_active or not destination.is_active:
                raise serializers.ValidationError("لا يمكن التحويل من مخزن موقوف أو إلى مخزن موقوف.")
        if lines is not None:
            if not lines:
                raise serializers.ValidationError({"lines": "أضف صنفًا واحدًا على الأقل."})
            seen = set()
            for item in lines:
                product = item["product"]
                expiry_date = item.get("expiry_date")
                key = (product.pk, expiry_date)
                if key in seen:
                    raise serializers.ValidationError({"lines": "لا تكرر الصنف ونفس تاريخ الصلاحية في التحويل."})
                seen.add(key)
                if company and product.company_id != company.pk:
                    raise serializers.ValidationError({"lines": "يوجد صنف لا يتبع شركة التحويل."})
                if product.product_type != Product.ProductType.GOODS or not product.is_active:
                    raise serializers.ValidationError({"lines": "التحويل متاح للأصناف المخزنية النشطة فقط."})
                if product.track_expiry and not expiry_date:
                    raise serializers.ValidationError({"lines": f"تاريخ الصلاحية مطلوب للصنف {product.code}."})
                if not product.track_expiry and expiry_date:
                    raise serializers.ValidationError({"lines": f"الصنف {product.code} لا يتطلب تاريخ صلاحية."})
                quantum = Decimal("1").scaleb(-product.unit.decimal_places)
                if item["quantity"] != item["quantity"].quantize(quantum):
                    raise serializers.ValidationError({"lines": f"كمية الصنف {product.code} تتجاوز دقة وحدة القياس."})
        if self.instance and "lines" in attrs and self.instance.dispatch_movement_id:
            raise serializers.ValidationError("لا يمكن تعديل بنود تحويل تم شحنه.")
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        lines = validated_data.pop("lines")
        transfer = StockTransfer.objects.create(**validated_data)
        StockTransferLine.objects.bulk_create([
            StockTransferLine(transfer=transfer, **line) for line in lines
        ])
        return transfer

    @transaction.atomic
    def update(self, instance, validated_data):
        lines = validated_data.pop("lines", None)
        for key, value in validated_data.items():
            setattr(instance, key, value)
        instance.full_clean()
        instance.save()
        if lines is not None:
            instance.lines.all().delete()
            StockTransferLine.objects.bulk_create([
                StockTransferLine(transfer=instance, **line) for line in lines
            ])
        return instance


class StockTransferReceiptInputLineSerializer(serializers.Serializer):
    transfer_line = serializers.IntegerField(min_value=1)
    quantity = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0.0001"))


class StockTransferReceiveSerializer(serializers.Serializer):
    receipt_date = serializers.DateField()
    lines = StockTransferReceiptInputLineSerializer(many=True)

    def validate_lines(self, value):
        if not value:
            raise serializers.ValidationError("أدخل بند استلام واحدًا على الأقل.")
        ids = [item["transfer_line"] for item in value]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError("لا تكرر بند التحويل في مستند الاستلام الواحد.")
        return value


class StockTransferReceiptLineReadSerializer(serializers.ModelSerializer):
    product_code = serializers.CharField(source="transfer_line.product.code", read_only=True)
    product_name = serializers.CharField(source="transfer_line.product.name_ar", read_only=True)

    class Meta:
        model = StockTransferReceiptLine
        fields = ("id", "transfer_line", "product_code", "product_name", "quantity", "movement_line")


class StockTransferReceiptSerializer(serializers.ModelSerializer):
    lines = StockTransferReceiptLineReadSerializer(many=True, read_only=True)
    movement_number = serializers.CharField(source="movement.movement_number", read_only=True)

    class Meta:
        model = StockTransferReceipt
        fields = ("id", "transfer", "receipt_number", "receipt_date", "movement", "movement_number", "received_by", "received_at", "lines")
