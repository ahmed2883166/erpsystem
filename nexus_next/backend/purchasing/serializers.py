from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from rest_framework import serializers

from approvals.models import FinancialApprovalRequest
from approvals.services import refresh_financial_approval

from accounting.models import ChartOfAccount, TaxCode
from companies.models import ExchangeRate
from treasury.models import TreasuryAccount

from inventory.models import Product, StockMovement, StockMovementLine, StorageLocation
from .models import PurchaseInvoice, PurchaseInvoiceLine, PurchaseOrder, PurchaseOrderLine, PurchaseOrderReceiptLine, PurchaseReceipt, SupplierPayment, SupplierReturn, SupplierReturnLine


class ReceiptLineInputSerializer(serializers.Serializer):
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())
    quantity = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0.0001"))
    unit_cost = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0"))
    destination_location = serializers.IntegerField(required=False, allow_null=True)
    expiry_date = serializers.DateField(required=False, allow_null=True)
    purchase_order_line = serializers.IntegerField(required=False, allow_null=True)

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
            "id", "company", "supplier", "supplier_name", "warehouse", "warehouse_name", "purchase_order",
            "receipt_number", "receipt_date", "supplier_delivery_ref", "notes",
            "movement_status", "movement_number", "line_items", "lines", "created_at",
        )
        read_only_fields = ("created_at", "movement_status", "movement_number", "line_items")

    def get_line_items(self, obj):
        """تجهيز تفاصيل الأصناف المرتبطة بالمستند بصيغة مناسبة للواجهة."""
        return [{
            "id": line.pk, "product": line.product_id, "product_code": line.product.code,
            "product_name": line.product.name_ar, "quantity": str(line.quantity),
            "unit_cost": str(line.unit_cost), "cost_amount": str(line.cost_amount),
            "expiry_date": line.expiry_date.isoformat() if line.expiry_date else None,
        } for line in obj.stock_movement.lines.select_related("product").all()]

    def validate(self, attrs):
        """التحقق من صحة المدخلات واتساق العلاقات قبل قبول بيانات المستند."""
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
        order = attrs.get("purchase_order")
        if order:
            if order.status not in (PurchaseOrder.Status.CONFIRMED, PurchaseOrder.Status.PARTIALLY_RECEIVED):
                raise serializers.ValidationError({"purchase_order": "أمر الشراء غير معتمد أو مكتمل الاستلام."})
            if order.company_id != company.pk or order.supplier_id != supplier.pk or order.warehouse_id != warehouse.pk:
                raise serializers.ValidationError({"purchase_order": "بيانات أمر الشراء لا تطابق الشركة أو المورد أو المخزن."})
        requested_by_line = {}
        for line in attrs["lines"]:
            if company and line["product"].company_id != company.pk:
                raise serializers.ValidationError({"lines": "كل الأصناف يجب أن تتبع نفس الشركة."})
            order_line_id = line.get("purchase_order_line")
            if order and not order_line_id:
                raise serializers.ValidationError({"lines": "حدد بند أمر الشراء لكل صنف مستلم."})
            if order_line_id:
                order_line = PurchaseOrderLine.objects.filter(pk=order_line_id, order=order).select_related("product").first()
                if not order_line or order_line.product_id != line["product"].pk:
                    raise serializers.ValidationError({"lines": "بند أمر الشراء لا يطابق الصنف المحدد."})
                line["purchase_order_line_obj"] = order_line
                requested_by_line[order_line.pk] = requested_by_line.get(order_line.pk, Decimal("0")) + line["quantity"]
            location_id = line.get("destination_location")
            if location_id:
                location = StorageLocation.objects.filter(
                    pk=location_id, warehouse=warehouse, is_active=True
                ).first()
                if not location:
                    raise serializers.ValidationError({"lines": "موقع التخزين غير نشط أو لا يتبع مخزن الاستلام."})
                line["destination_location_obj"] = location
        for order_line_id, requested in requested_by_line.items():
            order_line = next(x["purchase_order_line_obj"] for x in attrs["lines"] if x.get("purchase_order_line_obj") and x["purchase_order_line_obj"].pk == order_line_id)
            if requested > order_line.remaining_quantity:
                raise serializers.ValidationError({"lines": f"الكمية المطلوبة تتجاوز المتبقي في أمر الشراء للصنف {order_line.product.code}."})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        """إنشاء المستند وبنوده داخل معاملة ذرية لضمان اتساق البيانات."""
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
        movement_lines = StockMovementLine.objects.bulk_create([
            StockMovementLine(
                movement=movement, product=line["product"], quantity=line["quantity"],
                unit_cost=line["unit_cost"], destination_location=line.get("destination_location_obj"),
                expiry_date=line.get("expiry_date"),
            ) for line in lines
        ])
        receipt = PurchaseReceipt.objects.create(**validated_data, stock_movement=movement, created_by=user)
        for movement_line, line in zip(movement_lines, lines):
            if line.get("purchase_order_line_obj"):
                PurchaseOrderReceiptLine.objects.create(
                    receipt_line=movement_line, order_line=line["purchase_order_line_obj"],
                    quantity=line["quantity"],
                )
        return receipt


class PurchaseInvoiceLineInputSerializer(serializers.Serializer):
    receipt_line = serializers.PrimaryKeyRelatedField(
        queryset=StockMovementLine.objects.select_related("movement", "product")
    )
    unit_price = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0"), required=False)
    transaction_unit_price = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0"), required=False)
    tax_rate = serializers.DecimalField(
        max_digits=7, decimal_places=4, min_value=Decimal("0"),
        max_value=Decimal("100"), required=False, default=Decimal("0")
    )
    tax_code = serializers.PrimaryKeyRelatedField(queryset=TaxCode.objects.all(), required=False, allow_null=True)


class PurchaseInvoiceLineSerializer(serializers.ModelSerializer):
    product_code = serializers.CharField(source="receipt_line.product.code", read_only=True)
    product_name = serializers.CharField(source="receipt_line.product.name_ar", read_only=True)
    quantity = serializers.DecimalField(
        source="receipt_line.quantity", max_digits=18, decimal_places=4, read_only=True
    )
    recoverable_tax_amount = serializers.SerializerMethodField()
    nonrecoverable_tax_amount = serializers.SerializerMethodField()

    def get_recoverable_tax_amount(self, obj):
        if obj.tax_code_id and not obj.tax_code.input_tax_recoverable:
            return "0.0000"
        return str(obj.tax_amount)

    def get_nonrecoverable_tax_amount(self, obj):
        if obj.tax_code_id and not obj.tax_code.input_tax_recoverable:
            return str(obj.tax_amount)
        return "0.0000"

    class Meta:
        model = PurchaseInvoiceLine
        fields = (
            "id", "receipt_line", "product_code", "product_name", "quantity",
            "unit_price", "transaction_unit_price", "transaction_net_amount", "transaction_tax_amount", "transaction_total_amount", "tax_rate", "tax_code", "net_amount", "tax_amount", "recoverable_tax_amount", "nonrecoverable_tax_amount", "total_amount",
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
    tax_allocations = serializers.SerializerMethodField()

    def get_tax_allocations(self, obj):
        return [{
            "branch": allocation.branch_id,
            "branch_name": allocation.branch.name_ar,
            "journal_entry": allocation.journal_entry_id,
            "inventory_amount": str(allocation.inventory_amount),
            "cogs_amount": str(allocation.cogs_amount),
            "adjustment_loss_amount": str(allocation.adjustment_loss_amount),
        } for allocation in obj.tax_allocations.select_related("branch", "journal_entry").all()]

    class Meta:
        model = PurchaseInvoice
        fields = (
            "id", "company", "supplier", "supplier_name", "receipt", "receipt_number",
            "invoice_number", "invoice_date", "transaction_currency", "exchange_rate", "exchange_rate_date", "transaction_net_total", "transaction_tax_total", "transaction_grand_total", "status", "net_total", "tax_total",
            "grand_total", "paid_total", "balance_due", "tax_allocations", "notes", "journal_entry", "journal_number", "invoice_lines",
            "lines", "created_at", "posted_at",
        )
        read_only_fields = (
            "status", "transaction_net_total", "transaction_tax_total", "transaction_grand_total", "net_total", "tax_total", "grand_total", "journal_entry",
            "journal_number", "paid_total", "balance_due", "tax_allocations", "created_at", "posted_at", "invoice_lines",
        )

    def get_paid_total(self, obj):
        """حساب إجمالي المبالغ المسددة والمعتمدة للمستند."""
        if obj.transaction_currency_id:
            paid = sum(
                (payment.carrying_base_amount or Decimal("0") for payment in obj.payments.all() if payment.status == SupplierPayment.Status.POSTED),
                Decimal("0"),
            )
        else:
            paid = sum(
                (payment.amount for payment in obj.payments.all() if payment.status == SupplierPayment.Status.POSTED),
                Decimal("0"),
            )
        return str(paid)

    def get_balance_due(self, obj):
        """حساب الرصيد المتبقي بعد خصم الدفعات المرحلة."""
        if obj.transaction_currency_id:
            paid = sum(
                (payment.carrying_base_amount or Decimal("0") for payment in obj.payments.all() if payment.status == SupplierPayment.Status.POSTED),
                Decimal("0"),
            )
        else:
            paid = sum(
                (payment.amount for payment in obj.payments.all() if payment.status == SupplierPayment.Status.POSTED),
                Decimal("0"),
            )
        returned = obj.returns.filter(status=SupplierReturn.Status.POSTED).aggregate(
            total=Sum("grand_total")
        )["total"] or Decimal("0")
        return str(max(obj.grand_total - paid - returned, Decimal("0")))

    def validate(self, attrs):
        """التحقق من صحة المدخلات واتساق العلاقات قبل قبول بيانات المستند."""
        instance = self.instance
        company = attrs.get("company", instance.company if instance else None)
        supplier = attrs.get("supplier", instance.supplier if instance else None)
        if supplier and not supplier.is_active and not instance:
            raise serializers.ValidationError({"supplier": "المورد موقوف ولا يمكن إنشاء فاتورة جديدة له."})
        receipt = attrs.get("receipt", instance.receipt if instance else None)
        invoice_date = attrs.get("invoice_date", instance.invoice_date if instance else None)
        transaction_currency = attrs.get("transaction_currency", instance.transaction_currency if instance else None)
        exchange_rate = attrs.get("exchange_rate", instance.exchange_rate if instance else None)
        exchange_rate_date = attrs.get("exchange_rate_date", instance.exchange_rate_date if instance else None)

        if instance:
            if instance.status != PurchaseInvoice.Status.DRAFT:
                raise serializers.ValidationError("لا يمكن تعديل فاتورة مورد مرحلة.")
            immutable = {
                "company": instance.company_id,
                "supplier": instance.supplier_id,
                "receipt": instance.receipt_id,
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
            if instance.payments.filter(status=SupplierPayment.Status.POSTED).exists():
                raise serializers.ValidationError("لا يمكن تعديل فاتورة لها مدفوعات مرحلة.")

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

        if not receipt or receipt.stock_movement.status != StockMovement.Status.POSTED:
            raise serializers.ValidationError({"receipt": "يجب ترحيل إذن استلام البضاعة أولاً."})
        if company and receipt.company_id != company.pk:
            raise serializers.ValidationError({"receipt": "إذن الاستلام لا يتبع الشركة المحددة."})
        if supplier and not instance and not supplier.is_active:
            raise serializers.ValidationError({"supplier": "المورد موقوف ولا يمكن استخدامه في مستند جديد."})
        if supplier and receipt.supplier_id != supplier.pk:
            raise serializers.ValidationError({"supplier": "المورد لا يطابق مورد إذن الاستلام."})
        if receipt.stock_movement.movement_type != StockMovement.MovementType.RECEIPT:
            raise serializers.ValidationError({"receipt": "الإذن المرتبط ليس استلامًا مخزنيًا."})

        lines = attrs.get("lines")
        if lines is None:
            return attrs
        expected = set(receipt.stock_movement.lines.values_list("id", flat=True))
        supplied = [line["receipt_line"].pk for line in lines]
        if not lines or set(supplied) != expected or len(supplied) != len(expected):
            raise serializers.ValidationError({"lines": "يجب إدخال كل أصناف إذن الاستلام مرة واحدة دون حذف أو تكرار."})
        for line in lines:
            if transaction_currency and line.get("transaction_unit_price") is None:
                raise serializers.ValidationError({"lines": "سعر الوحدة بالعملة الأجنبية مطلوب لكل بند."})
            if not transaction_currency and line.get("unit_price") is None:
                raise serializers.ValidationError({"lines": "سعر الوحدة بالعملة الأساسية مطلوب لكل بند."})
            code = line.get("tax_code")
            if code and (code.company_id != company.pk or code.country != company.country or not code.is_active
                         or code.category == TaxCode.Category.REVERSE_CHARGE
                         or code.effective_from > invoice_date or (code.effective_to and code.effective_to < invoice_date)):
                raise serializers.ValidationError({"lines": "يوجد كود ضريبي غير صالح أو غير قابل للخصم للشركة أو تاريخ الفاتورة."})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        """إنشاء المستند وبنوده داخل معاملة ذرية لضمان اتساق البيانات."""
        lines = validated_data.pop("lines")
        invoice = PurchaseInvoice.objects.create(
            **validated_data, created_by=self.context["request"].user
        )
        for line in lines:
            PurchaseInvoiceLine.objects.create(
                invoice=invoice,
                receipt_line=line["receipt_line"],
                unit_price=line.get("unit_price", Decimal("0")),
                transaction_unit_price=line.get("transaction_unit_price"),
                tax_rate=line["tax_rate"],
                tax_code=line.get("tax_code"),
            )
        invoice.net_total = sum((line.net_amount for line in invoice.lines.all()), Decimal("0"))
        invoice.tax_total = sum((line.tax_amount for line in invoice.lines.all()), Decimal("0"))
        invoice.grand_total = invoice.net_total + invoice.tax_total
        if invoice.transaction_currency_id:
            invoice.transaction_net_total = sum((line.transaction_net_amount or Decimal("0") for line in invoice.lines.all()), Decimal("0"))
            invoice.transaction_tax_total = sum((line.transaction_tax_amount or Decimal("0") for line in invoice.lines.all()), Decimal("0"))
            invoice.transaction_grand_total = invoice.transaction_net_total + invoice.transaction_tax_total
            expected_base = (invoice.transaction_grand_total * invoice.exchange_rate).quantize(Decimal("0.0001"))
            if abs(expected_base - invoice.grand_total) > Decimal("0.0002") * max(invoice.lines.count(), 1):
                raise serializers.ValidationError("إجمالي الفاتورة الأساسي لا يطابق الإجمالي الأجنبي مضروبًا في سعر الصرف.")
        invoice.save(update_fields=("net_total", "tax_total", "grand_total", "transaction_net_total", "transaction_tax_total", "transaction_grand_total"))
        refresh_financial_approval(
            FinancialApprovalRequest.DocumentType.PURCHASE_INVOICE,
            invoice, self.context["request"].user,
        )
        return invoice

    @transaction.atomic
    def update(self, instance, validated_data):
        """تحديث المستند وبنوده مع حماية القيود والمستندات المرحلة."""
        invoice = PurchaseInvoice.objects.select_for_update().get(pk=instance.pk)
        if invoice.status != PurchaseInvoice.Status.DRAFT:
            raise serializers.ValidationError("لا يمكن تعديل فاتورة مورد مرحلة.")
        if invoice.payments.filter(status=SupplierPayment.Status.POSTED).exists():
            raise serializers.ValidationError("لا يمكن تعديل فاتورة لها مدفوعات مرحلة.")

        lines = validated_data.pop("lines", None)
        invoice = super().update(invoice, validated_data)
        if lines is not None:
            existing = {line.receipt_line_id: line for line in invoice.lines.select_for_update()}
            for data in lines:
                receipt_line = data["receipt_line"]
                line = existing.get(receipt_line.pk)
                if line is None:
                    raise serializers.ValidationError({"lines": "تعذر مطابقة سطر الفاتورة مع سطر الاستلام."})
                line.unit_price = data.get("unit_price", line.unit_price)
                line.transaction_unit_price = data.get("transaction_unit_price", line.transaction_unit_price)
                line.tax_rate = data.get("tax_rate", line.tax_rate)
                line.tax_code = data.get("tax_code", line.tax_code)
                line.save()

            invoice.net_total = sum((line.net_amount for line in invoice.lines.all()), Decimal("0"))
            invoice.tax_total = sum((line.tax_amount for line in invoice.lines.all()), Decimal("0"))
            invoice.grand_total = invoice.net_total + invoice.tax_total
            update_fields = ["net_total", "tax_total", "grand_total"]
            if invoice.transaction_currency_id:
                invoice.transaction_net_total = sum(
                    (line.transaction_net_amount or Decimal("0") for line in invoice.lines.all()),
                    Decimal("0"),
                )
                invoice.transaction_tax_total = sum(
                    (line.transaction_tax_amount or Decimal("0") for line in invoice.lines.all()),
                    Decimal("0"),
                )
                invoice.transaction_grand_total = (
                    invoice.transaction_net_total + invoice.transaction_tax_total
                )
                expected_base = (
                    invoice.transaction_grand_total * invoice.exchange_rate
                ).quantize(Decimal("0.0001"))
                if abs(expected_base - invoice.grand_total) > Decimal("0.0002") * max(invoice.lines.count(), 1):
                    raise serializers.ValidationError(
                        "إجمالي الفاتورة الأساسي لا يطابق الإجمالي الأجنبي مضروبًا في سعر الصرف."
                    )
                update_fields.extend((
                    "transaction_net_total", "transaction_tax_total", "transaction_grand_total",
                ))
            invoice.save(update_fields=update_fields)

        refresh_financial_approval(
            FinancialApprovalRequest.DocumentType.PURCHASE_INVOICE,
            invoice, self.context["request"].user,
        )
        return invoice


class SupplierPaymentSerializer(serializers.ModelSerializer):
    payment_account = serializers.PrimaryKeyRelatedField(queryset=ChartOfAccount.objects.all(), required=False, allow_null=True)
    supplier_name = serializers.CharField(source="supplier.name_ar", read_only=True)
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
        model = SupplierPayment
        fields = (
            "id", "company", "supplier", "supplier_name", "invoice", "invoice_number",
            "payment_number", "payment_date", "method", "payment_account", "payment_account_name", "amount", "transaction_currency", "transaction_amount", "exchange_rate", "exchange_rate_date", "carrying_base_amount", "historical_carrying_base_amount", "realized_fx_gain", "realized_fx_loss", "reference",
            "status", "journal_entry", "journal_number", "created_at", "posted_at",
            "reversal_journal_entry",
        )
        read_only_fields = ("status", "carrying_base_amount", "historical_carrying_base_amount", "realized_fx_gain", "realized_fx_loss", "journal_entry", "journal_number", "created_at", "posted_at")

    def validate(self, attrs):
        """التحقق من صحة المدخلات واتساق العلاقات قبل قبول بيانات المستند."""
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
        if invoice.transaction_currency_id:
            from accounting.fx_arap import validate_snapshot
            currency = attrs.get("transaction_currency")
            amount = attrs.get("transaction_amount")
            rate = attrs.get("exchange_rate")
            rate_date = attrs.get("exchange_rate_date")
            if currency != invoice.transaction_currency or amount is None or rate is None or rate_date is None:
                raise serializers.ValidationError({"transaction_currency": "أدخل عملة ومبلغ السداد وسعر الصرف وتاريخه بما يطابق فاتورة المورد."})
            approved = ExchangeRate.objects.filter(company=company, currency=currency, rate_date=rate_date).first()
            if not approved:
                raise serializers.ValidationError({"exchange_rate": "لا يوجد سعر صرف معتمد بتاريخ السداد."})
            try:
                base_amount = validate_snapshot(base_currency_code=company.base_currency.code,
                    currency_code=currency.code, foreign_amount=amount, rate=rate,
                    rate_date=rate_date, document_date=attrs.get("payment_date"), approved_rate=approved.rate)
            except ValueError as exc:
                raise serializers.ValidationError({"exchange_rate": str(exc)}) from exc
            if base_amount != attrs.get("amount"):
                raise serializers.ValidationError({"amount": "المبلغ الأساسي لا يطابق المبلغ الأجنبي مضروبًا في سعر الصرف."})
        elif any(attrs.get(field) is not None for field in ("transaction_currency", "transaction_amount", "exchange_rate", "exchange_rate_date")):
            raise serializers.ValidationError({"transaction_currency": "لا تستخدم بيانات عملة أجنبية مع فاتورة بالعملة الأساسية."})
        account = attrs.get("payment_account")
        if account:
            expected_kind = TreasuryAccount.Kind.CASH if attrs.get("method") == SupplierPayment.Method.CASH else TreasuryAccount.Kind.BANK
            treasury_account = TreasuryAccount.objects.filter(company=company, account=account, kind=expected_kind, is_active=True).first()
            branch_id = invoice.receipt.warehouse.branch_id
            if (account.company_id != company.pk or account.nature != ChartOfAccount.Nature.ASSET
                    or not account.is_active or account.is_group or not account.allow_posting
                    or not treasury_account
                    or (treasury_account.branch_id and treasury_account.branch_id != branch_id)
                    or treasury_account.currency_code != company.base_currency.code):
                raise serializers.ValidationError({"payment_account": "اختر حساب خزينة/بنك نشطًا تابعًا لفرع الفاتورة (أو عامًا) وبعملة الشركة وبنفس طريقة الصرف."})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        """إنشاء المستند وبنوده داخل معاملة ذرية لضمان اتساق البيانات."""
        payment = SupplierPayment.objects.create(
            **validated_data, created_by=self.context["request"].user
        )
        refresh_financial_approval(
            FinancialApprovalRequest.DocumentType.SUPPLIER_PAYMENT,
            payment, self.context["request"].user,
        )
        return payment


class PurchaseOrderLineInputSerializer(serializers.Serializer):
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())
    quantity = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0.0001"))
    unit_price = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0"))
    tax_rate = serializers.DecimalField(max_digits=7, decimal_places=4, min_value=0, max_value=100, required=False, default=Decimal("0"))


class PurchaseOrderSerializer(serializers.ModelSerializer):
    lines = PurchaseOrderLineInputSerializer(many=True, write_only=True)
    line_items = serializers.SerializerMethodField()
    supplier_name = serializers.CharField(source="supplier.name_ar", read_only=True)
    warehouse_name = serializers.CharField(source="warehouse.name_ar", read_only=True)

    class Meta:
        model = PurchaseOrder
        fields = ("id", "company", "supplier", "supplier_name", "warehouse", "warehouse_name",
                  "order_number", "order_date", "expected_date", "status", "notes", "lines",
                  "line_items", "created_at", "confirmed_at")
        read_only_fields = ("status", "line_items", "created_at", "confirmed_at")

    def get_line_items(self, obj):
        """تجهيز تفاصيل الأصناف المرتبطة بالمستند بصيغة مناسبة للواجهة."""
        return [{
            "id": line.pk, "product": line.product_id, "product_code": line.product.code,
            "product_name": line.product.name_ar, "quantity": str(line.quantity),
            "received_quantity": str(line.received_quantity), "remaining_quantity": str(line.remaining_quantity),
            "unit_price": str(line.unit_price), "tax_rate": str(line.tax_rate),
        } for line in obj.lines.select_related("product").all()]

    def validate(self, attrs):
        """التحقق من صحة المدخلات واتساق العلاقات قبل قبول بيانات المستند."""
        instance = self.instance
        company = attrs.get("company", instance.company if instance else None)
        supplier = attrs.get("supplier", instance.supplier if instance else None)
        if supplier and not supplier.is_active and not instance:
            raise serializers.ValidationError({"supplier": "المورد موقوف ولا يمكن إنشاء أمر شراء جديد له."})
        warehouse = attrs.get("warehouse", instance.warehouse if instance else None)

        if instance:
            if instance.status != PurchaseOrder.Status.DRAFT:
                raise serializers.ValidationError("لا يمكن تعديل أمر شراء معتمد أو مرتبط باستلام.")
            immutable = {
                "company": instance.company_id, "supplier": instance.supplier_id,
                "warehouse": instance.warehouse_id, "order_number": instance.order_number,
                "order_date": instance.order_date,
            }
            for field, current in immutable.items():
                if field in attrs:
                    supplied = attrs[field]
                    supplied = supplied.pk if hasattr(supplied, "pk") else supplied
                    if supplied != current:
                        raise serializers.ValidationError({field: "بيانات أمر الشراء الأساسية لا تتغير بعد إنشائه."})
        if supplier and (not company or supplier.company_id != company.pk or not supplier.is_active):
            raise serializers.ValidationError({"supplier": "اختر موردًا نشطًا تابعًا للشركة."})
        if warehouse and (not company or warehouse.company_id != company.pk or not warehouse.is_active):
            raise serializers.ValidationError({"warehouse": "اختر مخزنًا نشطًا تابعًا للشركة."})
        lines = attrs.get("lines")
        if lines is None:
            if instance:
                return attrs
            raise serializers.ValidationError({"lines": "أضف صنفًا واحدًا على الأقل."})
        if not lines:
            raise serializers.ValidationError({"lines": "أضف صنفًا واحدًا على الأقل."})
        for line in lines:
            product = line["product"]
            if not company or product.company_id != company.pk or product.product_type != Product.ProductType.GOODS or not product.is_active:
                raise serializers.ValidationError({"lines": "كل البنود يجب أن تكون أصنافًا مخزنية نشطة تابعة للشركة."})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        """إنشاء المستند وبنوده داخل معاملة ذرية لضمان اتساق البيانات."""
        lines = validated_data.pop("lines")
        order = PurchaseOrder.objects.create(**validated_data, created_by=self.context["request"].user)
        PurchaseOrderLine.objects.bulk_create([PurchaseOrderLine(order=order, **line) for line in lines])
        return order

    @transaction.atomic
    def update(self, instance, validated_data):
        """تحديث المستند وبنوده مع حماية القيود والمستندات المرحلة."""
        order = PurchaseOrder.objects.select_for_update().get(pk=instance.pk)
        if order.status != PurchaseOrder.Status.DRAFT:
            raise serializers.ValidationError("لا يمكن تعديل أمر شراء معتمد أو مرتبط باستلام.")
        lines = validated_data.pop("lines", None)
        if lines is not None:
            if PurchaseOrderReceiptLine.objects.filter(order_line__order=order).exists() or order.lines.filter(received_quantity__gt=0).exists():
                raise serializers.ValidationError({"lines": "لا يمكن تغيير بنود أمر شراء بدأ استلامه."})
        order = super().update(order, validated_data)
        if lines is not None:
            order.lines.all().delete()
            PurchaseOrderLine.objects.bulk_create([PurchaseOrderLine(order=order, **line) for line in lines])
        return order


class SupplierReturnLineInputSerializer(serializers.Serializer):
    invoice_line = serializers.PrimaryKeyRelatedField(queryset=PurchaseInvoiceLine.objects.select_related(
        "invoice", "receipt_line", "receipt_line__product"
    ))
    quantity = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0.0001"))


class SupplierReturnLineSerializer(serializers.ModelSerializer):
    product_code = serializers.CharField(source="invoice_line.receipt_line.product.code", read_only=True)
    product_name = serializers.CharField(source="invoice_line.receipt_line.product.name_ar", read_only=True)

    class Meta:
        model = SupplierReturnLine
        fields = (
            "id", "invoice_line", "product_code", "product_name", "quantity",
            "net_amount", "tax_amount", "recoverable_tax_amount", "nonrecoverable_tax_amount",
        )
        read_only_fields = fields


class SupplierReturnSerializer(serializers.ModelSerializer):
    return_number = serializers.CharField(max_length=37)
    lines = SupplierReturnLineInputSerializer(many=True, write_only=True)
    return_lines = SupplierReturnLineSerializer(source="lines", many=True, read_only=True)
    supplier_name = serializers.CharField(source="supplier.name_ar", read_only=True)
    invoice_number = serializers.CharField(source="invoice.invoice_number", read_only=True)
    movement_status = serializers.CharField(source="stock_movement.status", read_only=True)
    journal_number = serializers.CharField(source="journal_entry.entry_number", read_only=True, allow_null=True)
    transaction_currency_code = serializers.CharField(source="invoice.transaction_currency.code", read_only=True, allow_null=True)
    transaction_exchange_rate = serializers.DecimalField(source="invoice.exchange_rate", max_digits=20, decimal_places=10, read_only=True, allow_null=True)

    class Meta:
        model = SupplierReturn
        fields = (
            "id", "company", "supplier", "supplier_name", "invoice", "invoice_number",
            "return_number", "return_date", "status", "net_amount", "tax_amount",
            "grand_total", "transaction_currency_code", "transaction_exchange_rate",
            "transaction_net_amount", "transaction_tax_amount", "transaction_grand_total",
            "movement_status", "journal_entry", "journal_number",
            "notes", "lines", "return_lines", "created_at", "posted_at",
        )
        read_only_fields = (
            "status", "net_amount", "tax_amount", "grand_total", "transaction_net_amount",
            "transaction_tax_amount", "transaction_grand_total", "transaction_currency_code",
            "transaction_exchange_rate", "movement_status",
            "journal_entry", "journal_number", "return_lines", "created_at", "posted_at",
        )

    def validate(self, attrs):
        company = attrs.get("company")
        supplier = attrs.get("supplier")
        invoice = attrs.get("invoice")
        return_date = attrs.get("return_date")
        lines = attrs.get("lines", [])
        if not invoice or invoice.status != PurchaseInvoice.Status.POSTED:
            raise serializers.ValidationError({"invoice": "اختر فاتورة مورد مرحلة."})
        if company and invoice.company_id != company.pk:
            raise serializers.ValidationError({"company": "الشركة لا تطابق فاتورة المورد."})
        if supplier and invoice.supplier_id != supplier.pk:
            raise serializers.ValidationError({"supplier": "المورد لا يطابق فاتورة المورد."})
        if return_date and return_date < invoice.invoice_date:
            raise serializers.ValidationError({"return_date": "تاريخ المرتجع لا يجوز أن يسبق الفاتورة."})
        if not lines:
            raise serializers.ValidationError({"lines": "أضف صنفًا واحدًا على الأقل."})
        seen = set()
        for line in lines:
            invoice_line = line["invoice_line"]
            if invoice_line.invoice_id != invoice.pk:
                raise serializers.ValidationError({"lines": "كل البنود يجب أن تتبع فاتورة المورد المحددة."})
            if invoice_line.pk in seen:
                raise serializers.ValidationError({"lines": "لا تكرر بند الفاتورة في نفس المرتجع."})
            seen.add(invoice_line.pk)
            returned = SupplierReturnLine.objects.filter(
                invoice_line=invoice_line,
                return_document__status=SupplierReturn.Status.POSTED,
            ).aggregate(total=Sum("quantity"))["total"] or Decimal("0")
            if returned + line["quantity"] > invoice_line.receipt_line.quantity:
                raise serializers.ValidationError({
                    "lines": f"كمية المرتجع تتجاوز المتبقي للصنف {invoice_line.receipt_line.product.code}."
                })
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        lines = validated_data.pop("lines")
        invoice = validated_data["invoice"]
        user = self.context["request"].user
        movement = StockMovement.objects.create(
            company=invoice.company,
            movement_number=f"SR-{validated_data['return_number']}",
            movement_type=StockMovement.MovementType.SUPPLIER_RETURN,
            movement_date=validated_data["return_date"],
            source_warehouse=invoice.receipt.warehouse,
            notes=validated_data.get("notes", ""),
            created_by=user,
        )
        movement_lines = StockMovementLine.objects.bulk_create([
            StockMovementLine(
                movement=movement,
                product=line["invoice_line"].receipt_line.product,
                quantity=line["quantity"],
                source_location=line["invoice_line"].receipt_line.destination_location,
                expiry_date=line["invoice_line"].receipt_line.expiry_date,
            ) for line in lines
        ])
        document = SupplierReturn.objects.create(
            **validated_data, stock_movement=movement, created_by=user,
        )
        SupplierReturnLine.objects.bulk_create([
            SupplierReturnLine(
                return_document=document,
                invoice_line=line["invoice_line"],
                movement_line=movement_line,
                quantity=line["quantity"],
            ) for movement_line, line in zip(movement_lines, lines)
        ])
        return document
