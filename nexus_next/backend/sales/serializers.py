from decimal import Decimal

from django.db import transaction
from rest_framework import serializers

from accounting.models import ChartOfAccount, TaxCode

from customers.models import Customer
from inventory.models import Product, StockMovement, StockMovementLine, StorageLocation, Warehouse
from .models import CustomerReceipt, SalesDelivery, SalesInvoice, SalesInvoiceLine
from approvals.snapshots import invoice_discount_snapshot


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
    tax_code = serializers.PrimaryKeyRelatedField(queryset=TaxCode.objects.all(), required=False, allow_null=True)
    discount_percent = serializers.DecimalField(max_digits=7, decimal_places=4, min_value=Decimal("0"), max_value=Decimal("100"), required=False, default=Decimal("0"))


class SalesInvoiceLineSerializer(serializers.ModelSerializer):
    product_code = serializers.CharField(source="delivery_line.product.code", read_only=True)
    product_name = serializers.CharField(source="delivery_line.product.name_ar", read_only=True)
    quantity = serializers.DecimalField(source="delivery_line.quantity", max_digits=18, decimal_places=4, read_only=True)

    class Meta:
        model = SalesInvoiceLine
        fields = ("id", "delivery_line", "product_code", "product_name", "quantity",
                  "unit_price", "tax_rate", "tax_code", "discount_percent", "discount_amount", "net_amount", "tax_amount", "total_amount")


class SalesInvoiceSerializer(serializers.ModelSerializer):
    lines = SalesInvoiceLineInputSerializer(many=True, write_only=True)
    invoice_lines = SalesInvoiceLineSerializer(source="lines", many=True, read_only=True)
    customer_name = serializers.CharField(source="customer.name_ar", read_only=True)
    delivery_number = serializers.CharField(source="delivery.delivery_number", read_only=True)
    journal_number = serializers.CharField(source="journal_entry.entry_number", read_only=True, allow_null=True)
    payment_account_name = serializers.CharField(source="payment_account.name_ar", read_only=True, allow_null=True)
    paid_total = serializers.SerializerMethodField()
    balance_due = serializers.SerializerMethodField()
    approval_status = serializers.SerializerMethodField()

    class Meta:
        model = SalesInvoice
        fields = ("id", "company", "customer", "customer_name", "delivery", "delivery_number",
                  "invoice_number", "invoice_date", "status", "net_total", "discount_total", "discount_approval_limit", "invoice_approval_limit", "tax_total", "grand_total",
                  "paid_total", "balance_due", "approval_status", "notes", "journal_entry", "journal_number",
                  "invoice_lines", "lines", "created_at", "posted_at")
        read_only_fields = ("status", "net_total", "discount_total", "discount_approval_limit", "invoice_approval_limit", "tax_total", "grand_total",
                            "balance_due", "journal_entry", "journal_number", "invoice_lines",
                            "created_at", "posted_at")

    def get_paid_total(self, obj):
        paid = sum((r.amount for r in obj.receipts.all() if r.status == CustomerReceipt.Status.POSTED), Decimal("0"))
        return str(paid)

    def get_balance_due(self, obj):
        paid = sum((r.amount for r in obj.receipts.all() if r.status == CustomerReceipt.Status.POSTED), Decimal("0"))
        return str(max(obj.grand_total - paid, Decimal("0")))

    def get_approval_status(self, obj):
        limit = obj.discount_approval_limit
        lines = list(obj.lines.all())
        discount_exceeded = limit is not None and any(line.discount_percent > limit for line in lines)
        amount_exceeded = (
            obj.invoice_approval_limit is not None
            and obj.grand_total > obj.invoice_approval_limit
        )
        if not discount_exceeded and not amount_exceeded:
            return "not_required"
        attempts = list(obj.approval_requests.all())
        approval = max(attempts, key=lambda row: (row.requested_at, row.pk), default=None)
        if not approval:
            return "pending"
        max_discount = max((line.discount_percent for line in lines), default=Decimal("0"))
        if (
            approval.requested_discount_total != obj.discount_total
            or approval.requested_max_discount_percent != max_discount
            or approval.requested_invoice_limit != obj.invoice_approval_limit
            or approval.requested_lines_snapshot != invoice_discount_snapshot(obj)
        ):
            return "pending"
        return approval.status

    def validate(self, attrs):
        instance = self.instance
        company = attrs.get("company", instance.company if instance else None)
        customer = attrs.get("customer", instance.customer if instance else None)
        delivery = attrs.get("delivery", instance.delivery if instance else None)
        invoice_date = attrs.get("invoice_date", instance.invoice_date if instance else None)

        if instance:
            if instance.status != SalesInvoice.Status.DRAFT:
                raise serializers.ValidationError("لا يمكن تعديل فاتورة مبيعات مرحلة.")
            immutable = {
                "company": instance.company_id,
                "customer": instance.customer_id,
                "delivery": instance.delivery_id,
                "invoice_number": instance.invoice_number,
                "invoice_date": instance.invoice_date,
            }
            for field, current in immutable.items():
                if field in attrs:
                    supplied = attrs[field]
                    supplied = supplied.pk if hasattr(supplied, "pk") else supplied
                    if supplied != current:
                        raise serializers.ValidationError({field: "بيانات الفاتورة الأساسية لا تتغير بعد إنشائها."})
            if "lines" not in attrs:
                return attrs

        if not delivery or delivery.stock_movement.status != StockMovement.Status.POSTED:
            raise serializers.ValidationError({"delivery": "يجب ترحيل إذن التسليم أولًا."})
        if delivery.company_id != company.pk or delivery.customer_id != customer.pk:
            raise serializers.ValidationError({"delivery": "إذن التسليم لا يتبع العميل والشركة."})
        if not customer.is_active:
            raise serializers.ValidationError({"customer": "العميل موقوف."})

        lines = attrs.get("lines", [])
        expected = set(delivery.stock_movement.lines.values_list("id", flat=True))
        supplied = [line["delivery_line"].pk for line in lines]
        if not supplied or set(supplied) != expected or len(supplied) != len(expected):
            raise serializers.ValidationError({"lines": "يجب إدخال كل أصناف إذن التسليم مرة واحدة دون حذف أو تكرار."})
        for line in lines:
            code = line.get("tax_code")
            if code and (
                code.company_id != company.pk or code.country != company.country or not code.is_active
                or code.category == TaxCode.Category.REVERSE_CHARGE
                or code.effective_from > invoice_date
                or (code.effective_to and code.effective_to < invoice_date)
            ):
                raise serializers.ValidationError({"lines": "يوجد كود ضريبي غير صالح للشركة أو تاريخ الفاتورة."})
        return attrs

    def _recalculate_invoice(self, invoice, lines_changed):
        from approvals.models import SalesInvoiceApproval
        invoice_lines = list(invoice.lines.all())
        invoice.net_total = sum((line.net_amount for line in invoice_lines), Decimal("0"))
        invoice.discount_total = sum((line.discount_amount for line in invoice_lines), Decimal("0"))
        invoice.tax_total = sum((line.tax_amount for line in invoice_lines), Decimal("0"))
        invoice.grand_total = invoice.net_total + invoice.tax_total
        update_fields = ["net_total", "discount_total", "tax_total", "grand_total"]
        invoice.save(update_fields=update_fields)

        if lines_changed and (
            invoice.discount_approval_limit is not None or invoice.invoice_approval_limit is not None
        ):
            for old_request in invoice.approval_requests.filter(status=SalesInvoiceApproval.Status.PENDING):
                old_request.status = SalesInvoiceApproval.Status.SUPERSEDED
                old_request.decision_note = "تم تعديل الفاتورة؛ أُنشئ طلب أحدث عند الحاجة."
                old_request.save()
            max_discount = max((line.discount_percent for line in invoice_lines), default=Decimal("0"))
            discount_exceeded = (
                invoice.discount_approval_limit is not None
                and max_discount > invoice.discount_approval_limit
            )
            amount_exceeded = (
                invoice.invoice_approval_limit is not None
                and invoice.grand_total > invoice.invoice_approval_limit
            )
            if discount_exceeded or amount_exceeded:
                SalesInvoiceApproval.objects.create(
                    company=invoice.company,
                    invoice=invoice,
                    requested_by=self.context["request"].user,
                    requested_limit=invoice.discount_approval_limit,
                    requested_invoice_limit=invoice.invoice_approval_limit,
                    requested_discount_total=invoice.discount_total,
                    requested_max_discount_percent=max_discount,
                    requested_lines_snapshot=invoice_discount_snapshot(invoice),
                )

    @transaction.atomic
    def create(self, validated_data):
        from approvals.models import DiscountApprovalPolicy
        lines = validated_data.pop("lines")
        invoice = SalesInvoice.objects.create(**validated_data, created_by=self.context["request"].user)
        policy = DiscountApprovalPolicy.objects.filter(company=invoice.company).first()
        if policy:
            invoice.discount_approval_limit = policy.max_discount_percent
            invoice.invoice_approval_limit = policy.max_sales_invoice_amount
            invoice.save(update_fields=("discount_approval_limit", "invoice_approval_limit"))
        for line in lines:
            SalesInvoiceLine.objects.create(invoice=invoice, **line)
        self._recalculate_invoice(invoice, lines_changed=True)
        return invoice

    @transaction.atomic
    def update(self, instance, validated_data):
        instance = SalesInvoice.objects.select_for_update().get(pk=instance.pk)
        self.instance = instance
        if instance.status != SalesInvoice.Status.DRAFT:
            raise serializers.ValidationError("لا يمكن تعديل فاتورة مبيعات مرحلة.")
        lines = validated_data.pop("lines", None)
        if lines is not None:
            existing = {line.delivery_line_id: line for line in instance.lines.all()}
            for data in lines:
                delivery_line = data.pop("delivery_line")
                line = existing[delivery_line.pk]
                line.unit_price = data.get("unit_price", line.unit_price)
                line.tax_rate = data.get("tax_rate", line.tax_rate)
                line.tax_code = data.get("tax_code", line.tax_code)
                line.discount_percent = data.get("discount_percent", line.discount_percent)
                line.save()
        invoice = super().update(instance, validated_data)
        if lines is not None:
            self._recalculate_invoice(invoice, lines_changed=True)
        return invoice


class CustomerReceiptSerializer(serializers.ModelSerializer):
    payment_account = serializers.PrimaryKeyRelatedField(queryset=ChartOfAccount.objects.all(), required=False, allow_null=True)
    customer_name = serializers.CharField(source="customer.name_ar", read_only=True)
    invoice_number = serializers.CharField(source="invoice.invoice_number", read_only=True)
    journal_number = serializers.CharField(source="journal_entry.entry_number", read_only=True, allow_null=True)

    class Meta:
        model = CustomerReceipt
        fields = ("id", "company", "customer", "customer_name", "invoice", "invoice_number",
                  "receipt_number", "receipt_date", "method", "payment_account", "payment_account_name", "amount", "reference", "status",
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
        account = attrs.get("payment_account")
        if account and (account.company_id != company.pk or account.nature != ChartOfAccount.Nature.ASSET or not account.is_active or account.is_group or not account.allow_posting):
            raise serializers.ValidationError({"payment_account": "اختر حساب خزينة/بنك نشطًا وقابلًا للترحيل تابعًا للشركة."})
        return attrs

    def create(self, validated_data):
        return CustomerReceipt.objects.create(**validated_data, created_by=self.context["request"].user)
