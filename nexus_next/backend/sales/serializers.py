from decimal import Decimal

from django.db import transaction
from rest_framework import serializers

from customers.models import Customer
from inventory.models import Product, StockMovement, StockMovementLine, StorageLocation, Warehouse
from .models import CustomerReceipt, SalesDelivery, SalesInvoice, SalesInvoiceLine


class DeliveryLineInputSerializer(serializers.Serializer):
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())
    quantity = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0.0001"))
    source_location = serializers.IntegerField(required=False, allow_null=True)
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


class SalesDeliverySerializer(serializers.ModelSerializer):
    delivery_number = serializers.CharField(max_length=37)
    lines = DeliveryLineInputSerializer(many=True, write_only=True)
    movement_status = serializers.CharField(source="stock_movement.status", read_only=True)
    customer_name = serializers.CharField(source="customer.name_ar", read_only=True)
    warehouse_name = serializers.CharField(source="warehouse.name_ar", read_only=True)
    line_items = serializers.SerializerMethodField()

    class Meta:
        model = SalesDelivery
        fields = ("id", "company", "customer", "customer_name", "warehouse", "warehouse_name",
                  "delivery_number", "delivery_date", "customer_reference", "notes",
                  "movement_status", "line_items", "lines", "created_at")
        read_only_fields = ("movement_status", "line_items", "created_at")

    def get_line_items(self, obj):
        return [{
            "id": line.pk, "product": line.product_id, "product_code": line.product.code,
            "product_name": line.product.name_ar, "quantity": str(line.quantity),
            "expiry_date": line.expiry_date.isoformat() if line.expiry_date else None,
        } for line in obj.stock_movement.lines.select_related("product").all()]

    def validate(self, attrs):
        company, customer, warehouse = attrs.get("company"), attrs.get("customer"), attrs.get("warehouse")
        if customer and (customer.company_id != company.pk or not customer.is_active):
            raise serializers.ValidationError({"customer": "اختر عميلًا نشطًا تابعًا للشركة."})
        if warehouse and (warehouse.company_id != company.pk or not warehouse.is_active):
            raise serializers.ValidationError({"warehouse": "اختر مخزنًا نشطًا تابعًا للشركة."})
        lines = attrs.get("lines", [])
        if not lines:
            raise serializers.ValidationError({"lines": "أضف صنفًا واحدًا على الأقل."})
        for line in lines:
            if line["product"].company_id != company.pk:
                raise serializers.ValidationError({"lines": "كل الأصناف يجب أن تتبع الشركة."})
            location_id = line.get("source_location")
            if location_id:
                location = StorageLocation.objects.filter(
                    pk=location_id, warehouse=warehouse, is_active=True
                ).first()
                if not location:
                    raise serializers.ValidationError({"lines": "موقع الصرف غير نشط أو لا يتبع المخزن."})
                line["source_location_obj"] = location
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        lines = validated_data.pop("lines")
        company, warehouse = validated_data["company"], validated_data["warehouse"]
        user = self.context["request"].user
        movement = StockMovement.objects.create(
            company=company, movement_number=f"SD-{validated_data['delivery_number']}",
            movement_type=StockMovement.MovementType.ISSUE, movement_date=validated_data["delivery_date"],
            source_warehouse=warehouse, notes=validated_data.get("notes", ""), created_by=user,
        )
        StockMovementLine.objects.bulk_create([
            StockMovementLine(
                movement=movement, product=line["product"], quantity=line["quantity"],
                source_location=line.get("source_location_obj"), expiry_date=line.get("expiry_date"),
            ) for line in lines
        ])
        return SalesDelivery.objects.create(**validated_data, stock_movement=movement, created_by=user)


class SalesInvoiceLineInputSerializer(serializers.Serializer):
    delivery_line = serializers.PrimaryKeyRelatedField(queryset=StockMovementLine.objects.select_related("movement", "product"))
    unit_price = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0"))
    tax_rate = serializers.DecimalField(max_digits=7, decimal_places=4, min_value=Decimal("0"),
                                        max_value=Decimal("100"), required=False, default=Decimal("0"))


class SalesInvoiceLineSerializer(serializers.ModelSerializer):
    product_code = serializers.CharField(source="delivery_line.product.code", read_only=True)
    product_name = serializers.CharField(source="delivery_line.product.name_ar", read_only=True)
    quantity = serializers.DecimalField(source="delivery_line.quantity", max_digits=18, decimal_places=4, read_only=True)

    class Meta:
        model = SalesInvoiceLine
        fields = ("id", "delivery_line", "product_code", "product_name", "quantity",
                  "unit_price", "tax_rate", "net_amount", "tax_amount", "total_amount")


class SalesInvoiceSerializer(serializers.ModelSerializer):
    lines = SalesInvoiceLineInputSerializer(many=True, write_only=True)
    invoice_lines = SalesInvoiceLineSerializer(source="lines", many=True, read_only=True)
    customer_name = serializers.CharField(source="customer.name_ar", read_only=True)
    delivery_number = serializers.CharField(source="delivery.delivery_number", read_only=True)
    journal_number = serializers.CharField(source="journal_entry.entry_number", read_only=True, allow_null=True)
    paid_total = serializers.SerializerMethodField()
    balance_due = serializers.SerializerMethodField()

    class Meta:
        model = SalesInvoice
        fields = ("id", "company", "customer", "customer_name", "delivery", "delivery_number",
                  "invoice_number", "invoice_date", "status", "net_total", "tax_total", "grand_total",
                  "paid_total", "balance_due", "notes", "journal_entry", "journal_number",
                  "invoice_lines", "lines", "created_at", "posted_at")
        read_only_fields = ("status", "net_total", "tax_total", "grand_total", "paid_total",
                            "balance_due", "journal_entry", "journal_number", "invoice_lines",
                            "created_at", "posted_at")

    def get_paid_total(self, obj):
        paid = sum((r.amount for r in obj.receipts.all() if r.status == CustomerReceipt.Status.POSTED), Decimal("0"))
        return str(paid)

    def get_balance_due(self, obj):
        paid = sum((r.amount for r in obj.receipts.all() if r.status == CustomerReceipt.Status.POSTED), Decimal("0"))
        return str(max(obj.grand_total - paid, Decimal("0")))

    def validate(self, attrs):
        company, customer, delivery = attrs.get("company"), attrs.get("customer"), attrs.get("delivery")
        if not delivery or delivery.stock_movement.status != StockMovement.Status.POSTED:
            raise serializers.ValidationError({"delivery": "يجب ترحيل إذن التسليم أولًا."})
        if delivery.company_id != company.pk or delivery.customer_id != customer.pk:
            raise serializers.ValidationError({"delivery": "إذن التسليم لا يتبع العميل والشركة."})
        if not customer.is_active:
            raise serializers.ValidationError({"customer": "العميل موقوف."})
        expected = set(delivery.stock_movement.lines.values_list("id", flat=True))
        supplied = [line["delivery_line"].pk for line in attrs.get("lines", [])]
        if not supplied or set(supplied) != expected or len(supplied) != len(expected):
            raise serializers.ValidationError({"lines": "يجب إدخال كل أصناف إذن التسليم مرة واحدة دون حذف أو تكرار."})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        lines = validated_data.pop("lines")
        invoice = SalesInvoice.objects.create(**validated_data, created_by=self.context["request"].user)
        for line in lines:
            SalesInvoiceLine.objects.create(invoice=invoice, **line)
        invoice.net_total = sum((x.net_amount for x in invoice.lines.all()), Decimal("0"))
        invoice.tax_total = sum((x.tax_amount for x in invoice.lines.all()), Decimal("0"))
        invoice.grand_total = invoice.net_total + invoice.tax_total
        invoice.save(update_fields=("net_total", "tax_total", "grand_total"))
        return invoice


class CustomerReceiptSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source="customer.name_ar", read_only=True)
    invoice_number = serializers.CharField(source="invoice.invoice_number", read_only=True)
    journal_number = serializers.CharField(source="journal_entry.entry_number", read_only=True, allow_null=True)

    class Meta:
        model = CustomerReceipt
        fields = ("id", "company", "customer", "customer_name", "invoice", "invoice_number",
                  "receipt_number", "receipt_date", "method", "amount", "reference", "status",
                  "journal_entry", "journal_number", "created_at", "posted_at")
        read_only_fields = ("status", "journal_entry", "journal_number", "created_at", "posted_at")

    def validate(self, attrs):
        company, customer, invoice = attrs.get("company"), attrs.get("customer"), attrs.get("invoice")
        if not customer.is_active:
            raise serializers.ValidationError({"customer": "العميل موقوف."})
        if invoice.status != SalesInvoice.Status.POSTED:
            raise serializers.ValidationError({"invoice": "اختر فاتورة مبيعات مرحلة."})
        if invoice.company_id != company.pk or invoice.customer_id != customer.pk:
            raise serializers.ValidationError({"invoice": "الفاتورة لا تتبع العميل والشركة."})
        return attrs

    def create(self, validated_data):
        return CustomerReceipt.objects.create(**validated_data, created_by=self.context["request"].user)
