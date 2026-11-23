from decimal import Decimal

from django.db import transaction
from rest_framework import serializers

from accounting.models import ChartOfAccount, TaxCode
from companies.access import resolve_warehouse_branch
from companies.models import ExchangeRate
from treasury.models import TreasuryAccount

from inventory.models import Product, StockMovement, StockMovementLine, StorageLocation
from .models import CustomerReceipt, SalesDelivery, SalesInvoice, SalesInvoiceLine
from approvals.snapshots import invoice_discount_snapshot


class DeliveryLineInputSerializer(serializers.Serializer):
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())
    quantity = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0.0001"))
    source_location = serializers.IntegerField(required=False, allow_null=True)
    expiry_date = serializers.DateField(required=False, allow_null=True)

    def validate(self, attrs):
        """التحقق من صحة المدخلات واتساق العلاقات قبل قبول بيانات المستند."""
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
        """تجهيز تفاصيل الأصناف المرتبطة بالمستند بصيغة مناسبة للواجهة."""
        return [{
            "id": line.pk, "product": line.product_id, "product_code": line.product.code,
            "product_name": line.product.name_ar, "quantity": str(line.quantity),
            "expiry_date": line.expiry_date.isoformat() if line.expiry_date else None,
        } for line in obj.stock_movement.lines.select_related("product").all()]

    def validate(self, attrs):
        """التحقق من صحة المدخلات واتساق العلاقات قبل قبول بيانات المستند."""
        instance = self.instance
        company = attrs.get("company", instance.company if instance else None)
        customer = attrs.get("customer", instance.customer if instance else None)
        warehouse = attrs.get("warehouse", instance.warehouse if instance else None)
        if instance:
            if instance.stock_movement.status != StockMovement.Status.DRAFT:
                raise serializers.ValidationError("لا يمكن تعديل إذن تسليم مرحّل.")
            if company.pk != instance.company_id:
                raise serializers.ValidationError({"company": "لا يمكن نقل إذن التسليم إلى شركة أخرى."})
            if hasattr(instance, "invoice") and "lines" in attrs:
                raise serializers.ValidationError({"lines": "لا يمكن تغيير أصناف إذن تسليم مرتبط بفاتورة."})
        if customer and (customer.company_id != company.pk or not customer.is_active):
            raise serializers.ValidationError({"customer": "اختر عميلًا نشطًا تابعًا للشركة."})
        if warehouse and (warehouse.company_id != company.pk or not warehouse.is_active):
            raise serializers.ValidationError({"warehouse": "اختر مخزنًا نشطًا تابعًا للشركة."})
        lines = attrs.get("lines")
        if lines is None:
            if instance:
                return attrs
            raise serializers.ValidationError({"lines": "أضف صنفًا واحدًا على الأقل."})
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
        """إنشاء المستند وبنوده داخل معاملة ذرية لضمان اتساق البيانات."""
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

    @transaction.atomic
    def update(self, instance, validated_data):
        """تحديث المستند وبنوده مع حماية القيود والمستندات المرحلة."""
        delivery = SalesDelivery.objects.select_for_update().select_related("stock_movement").get(pk=instance.pk)
        movement = StockMovement.objects.select_for_update().get(pk=delivery.stock_movement_id)
        if movement.status != StockMovement.Status.DRAFT:
            raise serializers.ValidationError("لا يمكن تعديل إذن تسليم مرحّل.")
        lines = validated_data.pop("lines", None)
        if lines is not None and hasattr(delivery, "invoice"):
            raise serializers.ValidationError({"lines": "لا يمكن تغيير أصناف إذن تسليم مرتبط بفاتورة."})
        delivery = super().update(delivery, validated_data)
        movement.source_warehouse = delivery.warehouse
        movement.movement_date = delivery.delivery_date
        movement.notes = delivery.notes
        movement.save(update_fields=("source_warehouse", "movement_date", "notes"))
        if lines is not None:
            movement.lines.all().delete()
            StockMovementLine.objects.bulk_create([
                StockMovementLine(
                    movement=movement, product=line["product"], quantity=line["quantity"],
                    source_location=line.get("source_location_obj"), expiry_date=line.get("expiry_date"),
                ) for line in lines
            ])
        return delivery


class SalesInvoiceLineInputSerializer(serializers.Serializer):
    delivery_line = serializers.PrimaryKeyRelatedField(queryset=StockMovementLine.objects.select_related("movement", "product"))
    unit_price = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0"), required=False)
    transaction_unit_price = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0"), required=False)
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
                  "unit_price", "transaction_unit_price", "transaction_net_amount", "transaction_tax_amount", "transaction_total_amount", "tax_rate", "tax_code", "discount_percent", "discount_amount", "net_amount", "tax_amount", "total_amount")


class SalesInvoiceSerializer(serializers.ModelSerializer):
    lines = SalesInvoiceLineInputSerializer(many=True, write_only=True)
    invoice_lines = SalesInvoiceLineSerializer(source="lines", many=True, read_only=True)
    customer_name = serializers.CharField(source="customer.name_ar", read_only=True)
    delivery_number = serializers.CharField(source="delivery.delivery_number", read_only=True)
    journal_number = serializers.CharField(source="journal_entry.entry_number", read_only=True, allow_null=True)
    paid_total = serializers.SerializerMethodField()
    balance_due = serializers.SerializerMethodField()
    approval_status = serializers.SerializerMethodField()

    class Meta:
        model = SalesInvoice
        fields = ("id", "company", "customer", "customer_name", "delivery", "delivery_number",
                  "invoice_number", "invoice_date", "transaction_currency", "exchange_rate", "exchange_rate_date", "transaction_net_total", "transaction_tax_total", "transaction_grand_total", "status", "net_total", "discount_total", "discount_approval_limit", "invoice_approval_limit", "tax_total", "grand_total",
                  "paid_total", "balance_due", "approval_status", "notes", "journal_entry", "journal_number",
                  "invoice_lines", "lines", "created_at", "posted_at")
        read_only_fields = ("status", "transaction_net_total", "transaction_tax_total", "transaction_grand_total", "net_total", "discount_total", "discount_approval_limit", "invoice_approval_limit", "tax_total", "grand_total",
                            "balance_due", "journal_entry", "journal_number", "invoice_lines",
                            "created_at", "posted_at")

    def get_paid_total(self, obj):
        """حساب إجمالي المبالغ المسددة والمعتمدة للمستند."""
        if obj.transaction_currency_id:
            paid = sum((r.carrying_base_amount or Decimal("0") for r in obj.receipts.all() if r.status == CustomerReceipt.Status.POSTED), Decimal("0"))
        else:
            paid = sum((r.amount for r in obj.receipts.all() if r.status == CustomerReceipt.Status.POSTED), Decimal("0"))
        return str(paid)

    def get_balance_due(self, obj):
        """حساب الرصيد المتبقي بعد خصم الدفعات المرحلة."""
        if obj.transaction_currency_id:
            paid = sum((r.carrying_base_amount or Decimal("0") for r in obj.receipts.all() if r.status == CustomerReceipt.Status.POSTED), Decimal("0"))
        else:
            paid = sum((r.amount for r in obj.receipts.all() if r.status == CustomerReceipt.Status.POSTED), Decimal("0"))
        return str(max(obj.grand_total - paid, Decimal("0")))

    def get_approval_status(self, obj):
        """تحديد حالة الموافقة بناءً على حدود الاعتماد ولقطة الفاتورة الحالية."""
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
        """التحقق من صحة المدخلات واتساق العلاقات قبل قبول بيانات المستند."""
        instance = self.instance
        company = attrs.get("company", instance.company if instance else None)
        customer = attrs.get("customer", instance.customer if instance else None)
        delivery = attrs.get("delivery", instance.delivery if instance else None)
        invoice_date = attrs.get("invoice_date", instance.invoice_date if instance else None)
        transaction_currency = attrs.get("transaction_currency", instance.transaction_currency if instance else None)
        exchange_rate = attrs.get("exchange_rate", instance.exchange_rate if instance else None)
        exchange_rate_date = attrs.get("exchange_rate_date", instance.exchange_rate_date if instance else None)

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
            for field, current in (("transaction_currency", instance.transaction_currency_id), ("exchange_rate", instance.exchange_rate), ("exchange_rate_date", instance.exchange_rate_date)):
                if field in attrs:
                    supplied = attrs[field]
                    supplied = supplied.pk if hasattr(supplied, "pk") else supplied
                    if supplied != current:
                        raise serializers.ValidationError({field: "لا يمكن تغيير عملة أو سعر صرف الفاتورة بعد إنشائها."})
            if "lines" not in attrs:
                return attrs

        if not delivery or delivery.stock_movement.status != StockMovement.Status.POSTED:
            raise serializers.ValidationError({"delivery": "يجب ترحيل إذن التسليم أولًا."})
        if delivery.company_id != company.pk or delivery.customer_id != customer.pk:
            raise serializers.ValidationError({"delivery": "إذن التسليم لا يتبع العميل والشركة."})
        if not customer.is_active:
            raise serializers.ValidationError({"customer": "العميل موقوف."})

        if transaction_currency:
            if exchange_rate is None or exchange_rate_date is None:
                raise serializers.ValidationError({"exchange_rate": "حدد سعر الصرف وتاريخه للفاتورة الأجنبية."})
            if not transaction_currency.is_active or transaction_currency.code == company.base_currency.code:
                raise serializers.ValidationError({"transaction_currency": "اختر عملة أجنبية نشطة ومختلفة عن العملة الأساسية."})
            if exchange_rate <= 0 or exchange_rate_date > invoice_date:
                raise serializers.ValidationError({"exchange_rate_date": "سعر الصرف يجب أن يكون موجبًا وتاريخه لا يتجاوز تاريخ الفاتورة."})
            if not ExchangeRate.objects.filter(company=company, currency=transaction_currency, rate_date=exchange_rate_date, rate=exchange_rate).exists():
                raise serializers.ValidationError({"exchange_rate": "سعر الصرف لا يطابق سعرًا معتمدًا للشركة والتاريخ."})
        elif exchange_rate is not None or exchange_rate_date is not None:
            raise serializers.ValidationError({"transaction_currency": "حدد العملة الأجنبية قبل إدخال سعر الصرف."})

        lines = attrs.get("lines", [])
        expected = set(delivery.stock_movement.lines.values_list("id", flat=True))
        supplied = [line["delivery_line"].pk for line in lines]
        if not supplied or set(supplied) != expected or len(supplied) != len(expected):
            raise serializers.ValidationError({"lines": "يجب إدخال كل أصناف إذن التسليم مرة واحدة دون حذف أو تكرار."})
        for line in lines:
            if transaction_currency and line.get("transaction_unit_price") is None:
                raise serializers.ValidationError({"lines": "سعر الوحدة بالعملة الأجنبية مطلوب لكل بند."})
            if not transaction_currency and line.get("unit_price") is None:
                raise serializers.ValidationError({"lines": "سعر الوحدة بالعملة الأساسية مطلوب لكل بند."})
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
        """إعادة حساب إجماليات الفاتورة وتحديث طلبات الموافقة عند تغير البنود."""
        from approvals.models import SalesInvoiceApproval
        invoice_lines = list(invoice.lines.all())
        invoice.net_total = sum((line.net_amount for line in invoice_lines), Decimal("0"))
        invoice.discount_total = sum((line.discount_amount for line in invoice_lines), Decimal("0"))
        invoice.tax_total = sum((line.tax_amount for line in invoice_lines), Decimal("0"))
        invoice.grand_total = invoice.net_total + invoice.tax_total
        update_fields = ["net_total", "discount_total", "tax_total", "grand_total"]
        if invoice.transaction_currency_id:
            invoice.transaction_net_total = sum((line.transaction_net_amount or Decimal("0") for line in invoice_lines), Decimal("0"))
            invoice.transaction_tax_total = sum((line.transaction_tax_amount or Decimal("0") for line in invoice_lines), Decimal("0"))
            invoice.transaction_grand_total = invoice.transaction_net_total + invoice.transaction_tax_total
            expected_base = (invoice.transaction_grand_total * invoice.exchange_rate).quantize(Decimal("0.0001"))
            if abs(expected_base - invoice.grand_total) > Decimal("0.0002") * max(len(invoice_lines), 1):
                raise serializers.ValidationError("إجمالي الفاتورة الأساسي لا يطابق الإجمالي الأجنبي مضروبًا في سعر الصرف.")
            update_fields += ["transaction_net_total", "transaction_tax_total", "transaction_grand_total"]
        else:
            invoice.transaction_net_total = invoice.transaction_tax_total = invoice.transaction_grand_total = None
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
        """إنشاء المستند وبنوده داخل معاملة ذرية لضمان اتساق البيانات."""
        from approvals.models import DiscountApprovalPolicy
        lines = validated_data.pop("lines")
        invoice = SalesInvoice.objects.create(**validated_data, created_by=self.context["request"].user)
        policy = DiscountApprovalPolicy.objects.filter(company=invoice.company).first()
        if policy:
            invoice.discount_approval_limit = policy.max_discount_percent
            invoice.invoice_approval_limit = policy.max_sales_invoice_amount
            invoice.save(update_fields=("discount_approval_limit", "invoice_approval_limit"))
        for line in lines:
            SalesInvoiceLine.objects.create(
                invoice=invoice,
                unit_price=line.get("unit_price", Decimal("0")),
                transaction_unit_price=line.get("transaction_unit_price"),
                delivery_line=line["delivery_line"],
                tax_rate=line.get("tax_rate", Decimal("0")),
                tax_code=line.get("tax_code"),
                discount_percent=line.get("discount_percent", Decimal("0")),
            )
        self._recalculate_invoice(invoice, lines_changed=True)
        return invoice

    @transaction.atomic
    def update(self, instance, validated_data):
        """تحديث المستند وبنوده مع حماية القيود والمستندات المرحلة."""
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
                line.transaction_unit_price = data.get("transaction_unit_price", line.transaction_unit_price)
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
    payment_account_name = serializers.CharField(source="payment_account.name_ar", read_only=True, allow_null=True)

    reversal_journal_entry = serializers.SerializerMethodField()

    def get_reversal_journal_entry(self, obj):
        """إظهار رقم القيد العكسي المرتبط بقيد المستند عند وجوده."""
        if not obj.journal_entry_id:
            return None
        reversal = getattr(obj.journal_entry, "reversal_entry", None)
        return reversal.pk if reversal else None

    class Meta:
        model = CustomerReceipt
        fields = ("id", "company", "customer", "customer_name", "invoice", "invoice_number",
                  "receipt_number", "receipt_date", "method", "payment_account", "payment_account_name", "amount", "transaction_currency", "transaction_amount", "exchange_rate", "exchange_rate_date", "carrying_base_amount", "historical_carrying_base_amount", "realized_fx_gain", "realized_fx_loss", "reference", "status",
                  "journal_entry", "journal_number", "created_at", "posted_at", "reversal_journal_entry")
        read_only_fields = ("status", "carrying_base_amount", "historical_carrying_base_amount", "realized_fx_gain", "realized_fx_loss", "journal_entry", "journal_number", "created_at", "posted_at")

    def validate(self, attrs):
        """التحقق من صحة المدخلات واتساق العلاقات قبل قبول بيانات المستند."""
        company, customer, invoice = attrs.get("company"), attrs.get("customer"), attrs.get("invoice")
        if not customer.is_active:
            raise serializers.ValidationError({"customer": "العميل موقوف."})
        if invoice.status != SalesInvoice.Status.POSTED:
            raise serializers.ValidationError({"invoice": "اختر فاتورة مبيعات مرحلة."})
        if invoice.company_id != company.pk or invoice.customer_id != customer.pk:
            raise serializers.ValidationError({"invoice": "الفاتورة لا تتبع العميل والشركة."})
        if invoice.transaction_currency_id:
            from accounting.fx_arap import validate_snapshot
            currency = attrs.get("transaction_currency")
            amount = attrs.get("transaction_amount")
            rate = attrs.get("exchange_rate")
            rate_date = attrs.get("exchange_rate_date")
            if currency != invoice.transaction_currency or amount is None or rate is None or rate_date is None:
                raise serializers.ValidationError({"transaction_currency": "أدخل عملة ومبلغ التحصيل وسعر الصرف وتاريخه بما يطابق فاتورة العميل."})
            approved = ExchangeRate.objects.filter(company=company, currency=currency, rate_date=rate_date).first()
            if not approved:
                raise serializers.ValidationError({"exchange_rate": "لا يوجد سعر صرف معتمد بتاريخ التحصيل."})
            try:
                base_amount = validate_snapshot(base_currency_code=company.base_currency.code,
                    currency_code=currency.code, foreign_amount=amount, rate=rate,
                    rate_date=rate_date, document_date=attrs.get("receipt_date"), approved_rate=approved.rate)
            except ValueError as exc:
                raise serializers.ValidationError({"exchange_rate": str(exc)}) from exc
            if base_amount != attrs.get("amount"):
                raise serializers.ValidationError({"amount": "المبلغ الأساسي لا يطابق المبلغ الأجنبي مضروبًا في سعر الصرف."})
        elif any(attrs.get(field) is not None for field in ("transaction_currency", "transaction_amount", "exchange_rate", "exchange_rate_date")):
            raise serializers.ValidationError({"transaction_currency": "لا تستخدم بيانات عملة أجنبية مع فاتورة بالعملة الأساسية."})
        account = attrs.get("payment_account")
        if account:
            expected_kind = TreasuryAccount.Kind.CASH if attrs.get("method") == CustomerReceipt.Method.CASH else TreasuryAccount.Kind.BANK
            treasury_account = TreasuryAccount.objects.filter(company=company, account=account, kind=expected_kind, is_active=True).first()
            branch = resolve_warehouse_branch(company, invoice.delivery.warehouse)
            branch_id = branch.pk if branch else None
            if (account.company_id != company.pk or account.nature != ChartOfAccount.Nature.ASSET
                    or not account.is_active or account.is_group or not account.allow_posting
                    or not treasury_account
                    or (treasury_account.branch_id and treasury_account.branch_id != branch_id)
                    or treasury_account.currency_code != company.base_currency.code):
                raise serializers.ValidationError({"payment_account": "اختر حساب خزينة/بنك نشطًا تابعًا لفرع الفاتورة (أو عامًا) وبعملة الشركة وبنفس طريقة التحصيل."})
        return attrs

    def create(self, validated_data):
        """إنشاء المستند وبنوده داخل معاملة ذرية لضمان اتساق البيانات."""
        return CustomerReceipt.objects.create(**validated_data, created_by=self.context["request"].user)
