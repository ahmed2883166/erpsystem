from decimal import Decimal

from django.db import transaction
from rest_framework import serializers

from inventory.models import Product, StockMovement, StockMovementLine, StorageLocation
from .models import PurchaseInvoice, PurchaseInvoiceLine, PurchaseReceipt, SupplierPayment


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
    receipt_number = serializers.CharField(max_length=37)
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
        company = attrs.get("company")
        supplier = attrs.get("supplier")
        warehouse = attrs.get("warehouse")
        if supplier and not supplier.is_active:
            raise serializers.ValidationError({"supplier": "المورد موقوف ولا يمكن استخدامه في مستند جديد."})
        if supplier and company and supplier.company_id != company.pk:
            raise serializers.ValidationError({"supplier": "المورد لا يتبع الشركة المحددة."})
        if warehouse and company and warehouse.company_id != company.pk:
            raise serializers.ValidationError({"warehouse": "المخزن لا يتبع الشركة المحددة."})
        if warehouse and not warehouse.is_active:
            raise serializers.ValidationError({"warehouse": "مخزن الاستلام موقوف."})
        if not attrs.get("lines"):
            raise serializers.ValidationError({"lines": "أضف صنفًا واحدًا على الأقل."})
        for line in attrs["lines"]:
            if company and line["product"].company_id != company.pk:
                raise serializers.ValidationError({"lines": "كل الأصناف يجب أن تتبع نفس الشركة."})
            location_id = line.get("destination_location")
            if location_id:
                location = StorageLocation.objects.filter(
                    pk=location_id, warehouse=warehouse, is_active=True
                ).first()
                if not location:
                    raise serializers.ValidationError({"lines": "موقع التخزين غير نشط أو لا يتبع مخزن الاستلام."})
                line["destination_location_obj"] = location
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        lines = validated_data.pop("lines")
        company = validated_data["company"]
        warehouse = validated_data["warehouse"]
        user = self.context["request"].user
        movement = StockMovement.objects.create(
            company=company,
            movement_number=f"PR-{validated_data['receipt_number']}",
            movement_type=StockMovement.MovementType.RECEIPT,
            movement_date=validated_data["receipt_date"],
            destination_warehouse=warehouse,
            notes=validated_data.get("notes", ""),
            created_by=user,
        )
        StockMovementLine.objects.bulk_create([
            StockMovementLine(
                movement=movement,
                product=line["product"],
                quantity=line["quantity"],
                unit_cost=line["unit_cost"],
                destination_location=line.get("destination_location_obj"),
                expiry_date=line.get("expiry_date"),
            ) for line in lines
        ])
        return PurchaseReceipt.objects.create(
            **validated_data, stock_movement=movement, created_by=user
        )


class PurchaseInvoiceLineInputSerializer(serializers.Serializer):
    receipt_line = serializers.PrimaryKeyRelatedField(
        queryset=StockMovementLine.objects.select_related("movement", "product")
    )
    unit_price = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0"))
    tax_rate = serializers.DecimalField(
        max_digits=7, decimal_places=4, min_value=Decimal("0"),
        max_value=Decimal("100"), required=False, default=Decimal("0")
    )


class PurchaseInvoiceLineSerializer(serializers.ModelSerializer):
    product_code = serializers.CharField(source="receipt_line.product.code", read_only=True)
    product_name = serializers.CharField(source="receipt_line.product.name_ar", read_only=True)
    quantity = serializers.DecimalField(
        source="receipt_line.quantity", max_digits=18, decimal_places=4, read_only=True
    )

    class Meta:
        model = PurchaseInvoiceLine
        fields = (
            "id", "receipt_line", "product_code", "product_name", "quantity",
            "unit_price", "tax_rate", "net_amount", "tax_amount", "total_amount",
        )
        read_only_fields = (
            "id", "product_code", "product_name", "quantity",
            "net_amount", "tax_amount", "total_amount",
        )


class PurchaseInvoiceSerializer(serializers.ModelSerializer):
    lines = PurchaseInvoiceLineInputSerializer(many=True, write_only=True)
    invoice_lines = PurchaseInvoiceLineSerializer(source="lines", many=True, read_only=True)
    supplier_name = serializers.CharField(source="supplier.name_ar", read_only=True)
    receipt_number = serializers.CharField(source="receipt.receipt_number", read_only=True)
    journal_number = serializers.CharField(source="journal_entry.entry_number", read_only=True, allow_null=True)
    paid_total = serializers.SerializerMethodField()
    balance_due = serializers.SerializerMethodField()

    class Meta:
        model = PurchaseInvoice
        fields = (
            "id", "company", "supplier", "supplier_name", "receipt", "receipt_number",
            "invoice_number", "invoice_date", "status", "net_total", "tax_total",
            "grand_total", "paid_total", "balance_due", "notes", "journal_entry", "journal_number", "invoice_lines",
            "lines", "created_at", "posted_at",
        )
        read_only_fields = (
            "status", "net_total", "tax_total", "grand_total", "journal_entry",
            "journal_number", "paid_total", "balance_due", "created_at", "posted_at", "invoice_lines",
        )

    def get_paid_total(self, obj):
        paid = sum(
            (payment.amount for payment in obj.payments.all() if payment.status == SupplierPayment.Status.POSTED),
            Decimal("0"),
        )
        return str(paid)

    def get_balance_due(self, obj):
        paid = sum(
            (payment.amount for payment in obj.payments.all() if payment.status == SupplierPayment.Status.POSTED),
            Decimal("0"),
        )
        return str(max(obj.grand_total - paid, Decimal("0")))

    def validate(self, attrs):
        company = attrs.get("company")
        supplier = attrs.get("supplier")
        receipt = attrs.get("receipt")
        if not receipt or receipt.stock_movement.status != StockMovement.Status.POSTED:
            raise serializers.ValidationError({"receipt": "يجب ترحيل إذن استلام البضاعة أولاً."})
        if company and receipt.company_id != company.pk:
            raise serializers.ValidationError({"receipt": "إذن الاستلام لا يتبع الشركة المحددة."})
        if supplier and not supplier.is_active:
            raise serializers.ValidationError({"supplier": "المورد موقوف ولا يمكن استخدامه في مستند جديد."})
        if supplier and receipt.supplier_id != supplier.pk:
            raise serializers.ValidationError({"supplier": "المورد لا يطابق مورد إذن الاستلام."})
        if receipt.stock_movement.movement_type != StockMovement.MovementType.RECEIPT:
            raise serializers.ValidationError({"receipt": "الإذن المرتبط ليس استلامًا مخزنيًا."})
        lines = attrs.get("lines", [])
        expected = set(receipt.stock_movement.lines.values_list("id", flat=True))
        supplied = [line["receipt_line"].pk for line in lines]
        if not lines or set(supplied) != expected or len(supplied) != len(expected):
            raise serializers.ValidationError({"lines": "يجب إدخال كل أصناف إذن الاستلام مرة واحدة دون حذف أو تكرار."})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        lines = validated_data.pop("lines")
        invoice = PurchaseInvoice.objects.create(
            **validated_data, created_by=self.context["request"].user
        )
        for line in lines:
            PurchaseInvoiceLine.objects.create(
                invoice=invoice,
                receipt_line=line["receipt_line"],
                unit_price=line["unit_price"],
                tax_rate=line["tax_rate"],
            )
        invoice.net_total = sum((line.net_amount for line in invoice.lines.all()), Decimal("0"))
        invoice.tax_total = sum((line.tax_amount for line in invoice.lines.all()), Decimal("0"))
        invoice.grand_total = invoice.net_total + invoice.tax_total
        invoice.save(update_fields=("net_total", "tax_total", "grand_total"))
        return invoice


class SupplierPaymentSerializer(serializers.ModelSerializer):
    supplier_name = serializers.CharField(source="supplier.name_ar", read_only=True)
    invoice_number = serializers.CharField(source="invoice.invoice_number", read_only=True)
    journal_number = serializers.CharField(source="journal_entry.entry_number", read_only=True, allow_null=True)

    class Meta:
        model = SupplierPayment
        fields = (
            "id", "company", "supplier", "supplier_name", "invoice", "invoice_number",
            "payment_number", "payment_date", "method", "amount", "reference",
            "status", "journal_entry", "journal_number", "created_at", "posted_at",
        )
        read_only_fields = ("status", "journal_entry", "journal_number", "created_at", "posted_at")

    def validate(self, attrs):
        company = attrs.get("company")
        supplier = attrs.get("supplier")
        invoice = attrs.get("invoice")
        if supplier and not supplier.is_active:
            raise serializers.ValidationError({"supplier": "المورد موقوف."})
        if not invoice or invoice.status != PurchaseInvoice.Status.POSTED:
            raise serializers.ValidationError({"invoice": "اختر فاتورة مورد مرحلة."})
        if company and invoice.company_id != company.pk:
            raise serializers.ValidationError({"invoice": "الفاتورة لا تتبع الشركة المحددة."})
        if supplier and invoice.supplier_id != supplier.pk:
            raise serializers.ValidationError({"supplier": "المورد لا يطابق مورد الفاتورة."})
        return attrs

    def create(self, validated_data):
        return SupplierPayment.objects.create(
            **validated_data, created_by=self.context["request"].user
        )
