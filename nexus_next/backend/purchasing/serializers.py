from decimal import Decimal

from django.db import transaction
from django.db.models import Q, Sum
from rest_framework import serializers

from approvals.models import FinancialApprovalRequest
from approvals.services import refresh_financial_approval

from accounting.models import ChartOfAccount, CostCenter, FxSubledgerRevaluation, JournalEntry, TaxCode
from companies.access import resolve_warehouse_branch
from companies.models import Branch, ExchangeRate
from treasury.models import TreasuryAccount

from inventory.models import Product, StockMovement, StockMovementLine, StorageLocation
from suppliers.models import Supplier
from .models import LandedCostAllocation, LandedCostDocument, PurchaseInvoice, PurchaseInvoiceLine, PurchaseOrder, PurchaseOrderLine, PurchaseOrderReceiptLine, PurchaseReceipt, PurchaseRFQ, PurchaseRFQLine, PurchaseRFQInvitation, PurchaseServiceAcceptance, PurchaseServiceAcceptanceLine, SupplierQuotation, SupplierQuotationLine, SupplierPayment, SupplierRefund, SupplierReturn, SupplierReturnLine


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
            raise serializers.ValidationError("إذن الاستلام مخصص للأصناف المخزنية فقط؛ الخدمات تُثبت بمحضر إنجاز خدمة.")
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
    journal_number = serializers.CharField(source="journal_entry.entry_number", read_only=True, allow_null=True)
    supplier_name = serializers.CharField(source="supplier.name_ar", read_only=True)
    warehouse_name = serializers.CharField(source="warehouse.name_ar", read_only=True)
    line_items = serializers.SerializerMethodField()

    class Meta:
        model = PurchaseReceipt
        fields = (
            "id", "company", "supplier", "supplier_name", "warehouse", "warehouse_name", "purchase_order",
            "receipt_number", "receipt_date", "supplier_delivery_ref", "notes",
            "movement_status", "movement_number", "journal_number", "line_items", "lines", "created_at",
        )
        read_only_fields = ("created_at", "movement_status", "movement_number", "journal_number", "line_items")

    def get_line_items(self, obj):
        """تجهيز تفاصيل الأصناف المرتبطة بالمستند بصيغة مناسبة للواجهة."""
        return [{
            "id": line.pk, "product": line.product_id, "product_code": line.product.code,
            "product_name": line.product.name_ar, "product_type": line.product.product_type,
            "product_unit_weight": str(line.product.unit_weight), "product_unit_volume": str(line.product.unit_volume),
            "quantity": str(line.quantity),
            "unit_cost": str(line.unit_cost), "cost_amount": str(line.cost_amount),
            "expiry_date": line.expiry_date.isoformat() if line.expiry_date else None,
        } for line in obj.stock_movement.lines.select_related("product").all()]

    def validate(self, attrs):
        """التحقق من صحة المدخلات واتساق العلاقات قبل قبول بيانات المستند."""
        instance = self.instance
        company = attrs.get("company", instance.company if instance else None)
        supplier = attrs.get("supplier", instance.supplier if instance else None)
        warehouse = attrs.get("warehouse", instance.warehouse if instance else None)
        if instance:
            immutable = {
                "company": instance.company_id,
                "supplier": instance.supplier_id,
                "warehouse": instance.warehouse_id,
                "purchase_order": instance.purchase_order_id,
            }
            for field, current in immutable.items():
                if field in attrs:
                    supplied = attrs[field]
                    supplied = supplied.pk if hasattr(supplied, "pk") else supplied
                    if supplied != current:
                        raise serializers.ValidationError({field: "لا يمكن تغيير الشركة أو المورد أو المخزن أو أمر الشراء لإذن استلام قائم."})
            if instance.stock_movement.status != StockMovement.Status.DRAFT:
                raise serializers.ValidationError("إذن الاستلام المرحّل غير قابل للتعديل.")
            if PurchaseInvoice.objects.filter(receipt=instance).exists():
                raise serializers.ValidationError("لا يمكن تعديل إذن استلام مرتبط بفاتورة مورد.")
        if supplier and not supplier.is_active:
            raise serializers.ValidationError({"supplier": "المورد موقوف ولا يمكن استخدامه في مستند جديد."})
        if supplier and company and supplier.company_id != company.pk:
            raise serializers.ValidationError({"supplier": "المورد لا يتبع الشركة المحددة."})
        if warehouse and company and warehouse.company_id != company.pk:
            raise serializers.ValidationError({"warehouse": "المخزن لا يتبع الشركة المحددة."})
        if warehouse and not warehouse.is_active:
            raise serializers.ValidationError({"warehouse": "مخزن الاستلام موقوف."})
        if "lines" not in attrs and instance:
            return attrs
        if not attrs.get("lines"):
            raise serializers.ValidationError({"lines": "أضف صنفًا واحدًا على الأقل."})
        order = attrs.get("purchase_order", instance.purchase_order if instance else None)
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
                if order_line.unit_price > 0 and line["unit_cost"] != order_line.unit_price:
                    raise serializers.ValidationError({"lines": f"تكلفة استلام الصنف {order_line.product.code} يجب أن تطابق سعر أمر الشراء المعتمد."})
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
            reserved_query = PurchaseOrderReceiptLine.objects.filter(
                order_line_id=order_line_id,
                receipt_line__movement__status=StockMovement.Status.DRAFT,
            )
            if instance:
                reserved_query = reserved_query.exclude(receipt_line__movement_id=instance.stock_movement_id)
            reserved_drafts = reserved_query.aggregate(total=Sum("quantity"))["total"] or Decimal("0")
            available = order_line.remaining_quantity - reserved_drafts
            if requested > available:
                raise serializers.ValidationError({"lines": f"الكمية المطلوبة تتجاوز المتبقي بعد حجوزات أذون الاستلام المسودة للصنف {order_line.product.code}."})
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

    @transaction.atomic
    def update(self, instance, validated_data):
        """تعديل إذن استلام مسودة مع إعادة بناء بنود الحركة وحجوزات أمر الشراء بأمان."""
        lines = validated_data.pop("lines", None)
        movement = StockMovement.objects.select_for_update(of=("self",)).get(pk=instance.stock_movement_id)
        if movement.status != StockMovement.Status.DRAFT:
            raise serializers.ValidationError("إذن الاستلام المرحّل غير قابل للتعديل.")
        if PurchaseInvoice.objects.filter(receipt=instance).exists():
            raise serializers.ValidationError("لا يمكن تعديل إذن استلام مرتبط بفاتورة مورد.")
        if lines is not None:
            old_line_ids = list(movement.lines.values_list("id", flat=True))
            PurchaseOrderReceiptLine.objects.filter(receipt_line_id__in=old_line_ids).delete()
            movement.lines.all().delete()
            movement_lines = StockMovementLine.objects.bulk_create([
                StockMovementLine(
                    movement=movement, product=line["product"], quantity=line["quantity"],
                    unit_cost=line["unit_cost"], destination_location=line.get("destination_location_obj"),
                    expiry_date=line.get("expiry_date"),
                ) for line in lines
            ])
            for movement_line, line in zip(movement_lines, lines):
                order_line = line.get("purchase_order_line_obj")
                if order_line:
                    PurchaseOrderReceiptLine.objects.create(
                        receipt_line=movement_line, order_line=order_line, quantity=line["quantity"],
                    )
        for field in ("receipt_date", "notes"):
            if field in validated_data:
                setattr(movement, "movement_date" if field == "receipt_date" else field,
                        validated_data[field])
        movement.save(update_fields=("movement_date", "notes"))
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()
        return instance


class PurchaseInvoiceLineInputSerializer(serializers.Serializer):
    receipt_line = serializers.PrimaryKeyRelatedField(
        queryset=StockMovementLine.objects.select_related("movement", "product"),
        required=False, allow_null=True,
    )
    description = serializers.CharField(max_length=240, required=False, allow_blank=True)
    expense_account = serializers.PrimaryKeyRelatedField(queryset=ChartOfAccount.objects.all(), required=False, allow_null=True)
    cost_center = serializers.PrimaryKeyRelatedField(queryset=CostCenter.objects.all(), required=False, allow_null=True)
    billed_quantity = serializers.DecimalField(
        max_digits=18, decimal_places=4, min_value=Decimal("0.0001"), required=False,
    )
    unit_price = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0"), required=False)
    transaction_unit_price = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0"), required=False)
    tax_rate = serializers.DecimalField(
        max_digits=7, decimal_places=4, min_value=Decimal("0"),
        max_value=Decimal("100"), required=False, default=Decimal("0")
    )
    tax_code = serializers.PrimaryKeyRelatedField(queryset=TaxCode.objects.all(), required=False, allow_null=True)


class PurchaseInvoiceLineSerializer(serializers.ModelSerializer):
    product_code = serializers.CharField(source="receipt_line.product.code", read_only=True, allow_null=True)
    product_name = serializers.CharField(source="receipt_line.product.name_ar", read_only=True, allow_null=True)
    expense_account_name = serializers.CharField(source="expense_account.name_ar", read_only=True, allow_null=True)
    cost_center_name = serializers.CharField(source="cost_center.name_ar", read_only=True, allow_null=True)
    quantity = serializers.DecimalField(
        source="billed_quantity", max_digits=18, decimal_places=4, read_only=True
    )
    receipt_quantity = serializers.DecimalField(
        source="receipt_line.quantity", max_digits=18, decimal_places=4, read_only=True, allow_null=True
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
            "id", "receipt_line", "product_code", "product_name", "description", "expense_account", "expense_account_name",
            "cost_center", "cost_center_name", "quantity", "billed_quantity", "receipt_quantity",
            "unit_price", "transaction_unit_price", "transaction_net_amount", "transaction_tax_amount", "transaction_total_amount", "tax_rate", "tax_code", "net_amount", "tax_amount", "recoverable_tax_amount", "nonrecoverable_tax_amount", "total_amount",
        )
        read_only_fields = (
            "id", "product_code", "product_name", "quantity", "billed_quantity", "receipt_quantity",
            "net_amount", "tax_amount", "total_amount",
        )


class PurchaseInvoiceSerializer(serializers.ModelSerializer):
    lines = PurchaseInvoiceLineInputSerializer(many=True, write_only=True)
    invoice_lines = PurchaseInvoiceLineSerializer(source="lines", many=True, read_only=True)
    supplier_name = serializers.CharField(source="supplier.name_ar", read_only=True)
    receipt_number = serializers.CharField(source="receipt.receipt_number", read_only=True, allow_null=True)
    service_acceptance_number = serializers.CharField(source="service_acceptance.acceptance_number", read_only=True, allow_null=True)
    branch_name = serializers.CharField(source="branch.name_ar", read_only=True, allow_null=True)
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
            "id", "company", "supplier", "supplier_name", "activity", "invoice_type", "service_acceptance", "service_acceptance_number", "branch", "branch_name", "receipt", "receipt_number",
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
        refunded = SupplierRefund.objects.filter(
            return_document__invoice=obj,
            status=SupplierRefund.Status.POSTED,
        ).aggregate(total=Sum("amount"))["total"] or Decimal("0")
        return str(max(obj.grand_total - paid - returned + refunded, Decimal("0")))

    def validate(self, attrs):
        """التحقق من صحة المدخلات واتساق العلاقات قبل قبول بيانات المستند."""
        instance = self.instance
        company = attrs.get("company", instance.company if instance else None)
        supplier = attrs.get("supplier", instance.supplier if instance else None)
        receipt = attrs.get("receipt", instance.receipt if instance else None)
        invoice_type = attrs.get("invoice_type", instance.invoice_type if instance else PurchaseInvoice.InvoiceType.INVENTORY)
        branch = attrs.get("branch", instance.branch if instance else None)
        activity = attrs.get("activity", instance.activity if instance else None)
        if supplier and not supplier.is_active and not instance:
            raise serializers.ValidationError({"supplier": "المورد موقوف ولا يمكن إنشاء فاتورة جديدة له."})
        if activity and (
            not company or activity.company_id != company.pk or not activity.is_active
        ):
            raise serializers.ValidationError({"activity": "اختر نشاطًا مفعّلًا تابعًا للشركة."})
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
                "activity": instance.activity_id,
                "receipt": instance.receipt_id,
                "service_acceptance": instance.service_acceptance_id,
                "invoice_type": instance.invoice_type,
                "branch": instance.branch_id,
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

        lines = attrs.get("lines")
        linked_acceptance = attrs.get("service_acceptance", instance.service_acceptance if instance else None)
        if linked_acceptance and invoice_type != PurchaseInvoice.InvoiceType.DIRECT_EXPENSE:
            raise serializers.ValidationError({"service_acceptance": "محضر إنجاز الخدمة يرتبط بفاتورة خدمات ومصروفات مباشرة فقط."})
        if invoice_type == PurchaseInvoice.InvoiceType.DIRECT_EXPENSE:
            if receipt is not None:
                raise serializers.ValidationError({"receipt": "فاتورة الخدمات والمصروفات لا ترتبط بإذن استلام."})
            if not branch or branch.company_id != company.pk or not branch.is_active:
                raise serializers.ValidationError({"branch": "اختر فرعًا نشطًا تابعًا للشركة."})
            acceptance = attrs.get("service_acceptance", instance.service_acceptance if instance else None)
            if acceptance:
                if acceptance.status != PurchaseServiceAcceptance.Status.ACCEPTED:
                    raise serializers.ValidationError({"service_acceptance": "لا يمكن ربط الفاتورة إلا بمحضر إنجاز خدمة معتمد."})
                if acceptance.company_id != company.pk or acceptance.supplier_id != supplier.pk:
                    raise serializers.ValidationError({"service_acceptance": "محضر الإنجاز لا يتبع نفس الشركة والمورد."})
                acceptance_branch = resolve_warehouse_branch(company, acceptance.order.warehouse)
                if acceptance_branch and acceptance_branch.pk != branch.pk:
                    raise serializers.ValidationError({"branch": "يجب أن تستخدم فاتورة الخدمة نفس فرع أمر الشراء ومحضر الإنجاز."})
                if acceptance.acceptance_date > invoice_date:
                    raise serializers.ValidationError({"service_acceptance": "تاريخ الفاتورة لا يسبق تاريخ اعتماد إنجاز الخدمة."})
            elif attrs.get("service_acceptance") is not None:
                raise serializers.ValidationError({"service_acceptance": "محضر الإنجاز المحدد غير صالح."})
            if lines is None:
                return attrs
            if not lines:
                raise serializers.ValidationError({"lines": "أدخل بند خدمة أو مصروف واحدًا على الأقل."})
            for line in lines:
                if line.get("receipt_line") is not None:
                    raise serializers.ValidationError({"lines": "لا تستخدم سطر استلام في فاتورة مباشرة."})
                line["billed_quantity"] = line.get("billed_quantity") or Decimal("1")
                account = line.get("expense_account")
                if not account or account.company_id != company.pk or account.nature != ChartOfAccount.Nature.EXPENSE or not account.is_active or account.is_group or not account.allow_posting:
                    raise serializers.ValidationError({"lines": "كل بند مباشر يتطلب حساب مصروف نشطًا وتفصيليًا وقابلًا للترحيل."})
                cost_center = line.get("cost_center")
                if cost_center and (cost_center.company_id != company.pk or not cost_center.is_active):
                    raise serializers.ValidationError({"lines": "مركز التكلفة يجب أن يكون نشطًا ويتبع نفس الشركة."})
                if not str(line.get("description") or "").strip():
                    raise serializers.ValidationError({"lines": "وصف كل بند خدمة أو مصروف مطلوب."})
                if transaction_currency and line.get("transaction_unit_price") is None:
                    raise serializers.ValidationError({"lines": "سعر الوحدة بالعملة الأجنبية مطلوب لكل بند."})
                if not transaction_currency and line.get("unit_price") is None:
                    raise serializers.ValidationError({"lines": "قيمة البند بالعملة الأساسية مطلوبة."})
                code = line.get("tax_code")
                if code and (code.company_id != company.pk or code.country != company.country or not code.is_active
                             or code.category == TaxCode.Category.REVERSE_CHARGE
                             or code.effective_from > invoice_date or (code.effective_to and code.effective_to < invoice_date)):
                    raise serializers.ValidationError({"lines": "يوجد كود ضريبي غير صالح أو لا يسري على الشركة أو تاريخ الفاتورة."})
            return attrs

        if branch is not None:
            raise serializers.ValidationError({"branch": "فرع فاتورة المخزون يُحدد من مخزن إذن الاستلام."})
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

        if lines is None:
            return attrs
        if any(not line.get("receipt_line") for line in lines):
            raise serializers.ValidationError({"lines": "كل بند مخزني يجب أن يرتبط بسطر استلام."})
        if any(line.get("expense_account") or line.get("cost_center") for line in lines):
            raise serializers.ValidationError({"lines": "حساب المصروف ومركز التكلفة مخصصان لفواتير الخدمات والمصروفات المباشرة."})
        expected = set(receipt.stock_movement.lines.values_list("id", flat=True))
        supplied = [line["receipt_line"].pk for line in lines]
        if not lines or len(supplied) != len(set(supplied)):
            raise serializers.ValidationError({"lines": "أدخل بندًا واحدًا على الأقل دون تكرار سطر الاستلام."})
        if not set(supplied).issubset(expected):
            raise serializers.ValidationError({"lines": "يوجد سطر لا يتبع إذن الاستلام المحدد."})
        for line in lines:
            receipt_line = line["receipt_line"]
            if receipt_line.movement_id != receipt.stock_movement_id:
                raise serializers.ValidationError({"lines": "سطر الصنف لا يتبع إذن الاستلام المحدد."})
            previous = PurchaseInvoiceLine.objects.filter(
                receipt_line=receipt_line,
                invoice__status__in=(PurchaseInvoice.Status.DRAFT, PurchaseInvoice.Status.POSTED),
            ).exclude(invoice=instance).aggregate(quantity=Sum("billed_quantity"))["quantity"] or Decimal("0")
            existing_line = (
                instance.lines.filter(receipt_line=receipt_line).first()
                if instance else None
            )
            if "billed_quantity" not in line:
                line["billed_quantity"] = (
                    existing_line.billed_quantity if existing_line
                    else receipt_line.quantity - previous
                )
            if line["billed_quantity"] <= 0:
                raise serializers.ValidationError({
                    "lines": f"لا توجد كمية متبقية قابلة للفوترة للصنف {receipt_line.product.code}."
                })
            if line["billed_quantity"] > receipt_line.quantity - previous:
                raise serializers.ValidationError({
                    "lines": f"الكمية المفوترة أو المحجوزة تتجاوز المتبقي من استلام الصنف {receipt_line.product.code}."
                })
            if transaction_currency and line.get("transaction_unit_price") is None:
                raise serializers.ValidationError({"lines": "سعر الوحدة بالعملة الأجنبية مطلوب لكل بند."})
            if not transaction_currency and line.get("unit_price") is None:
                raise serializers.ValidationError({"lines": "سعر الوحدة بالعملة الأساسية مطلوب لكل بند."})
            code = line.get("tax_code")
            if code and (code.company_id != company.pk or code.country != company.country or not code.is_active
                         or code.category == TaxCode.Category.REVERSE_CHARGE
                         or code.effective_from > invoice_date or (code.effective_to and code.effective_to < invoice_date)):
                raise serializers.ValidationError({"lines": "يوجد كود ضريبي غير صالح أو لا يسري على الشركة أو تاريخ الفاتورة."})
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
                receipt_line=line.get("receipt_line"),
                description=line.get("description", ""),
                expense_account=line.get("expense_account"),
                cost_center=line.get("cost_center"),
                billed_quantity=line["billed_quantity"],
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
            if invoice.invoice_type == PurchaseInvoice.InvoiceType.DIRECT_EXPENSE:
                for old_line in invoice.lines.select_for_update():
                    old_line.delete()
                for data in lines:
                    PurchaseInvoiceLine.objects.create(
                        invoice=invoice, receipt_line=None,
                        description=data.get("description", ""),
                        expense_account=data.get("expense_account"),
                        cost_center=data.get("cost_center"),
                        billed_quantity=data.get("billed_quantity", Decimal("1")),
                        unit_price=data.get("unit_price", Decimal("0")),
                        transaction_unit_price=data.get("transaction_unit_price"),
                        tax_rate=data["tax_rate"], tax_code=data.get("tax_code"),
                    )
            else:
                existing = {line.receipt_line_id: line for line in invoice.lines.select_for_update()}
                supplied_ids = set()
                for data in lines:
                    receipt_line = data["receipt_line"]
                    supplied_ids.add(receipt_line.pk)
                    line = existing.get(receipt_line.pk)
                    if line is None:
                        line = PurchaseInvoiceLine(
                            invoice=invoice, receipt_line=receipt_line,
                            billed_quantity=data["billed_quantity"],
                            unit_price=data.get("unit_price", Decimal("0")),
                            transaction_unit_price=data.get("transaction_unit_price"),
                            tax_rate=data["tax_rate"], tax_code=data.get("tax_code"),
                        )
                    else:
                        line.billed_quantity = data["billed_quantity"]
                        line.unit_price = data.get("unit_price", line.unit_price)
                        line.transaction_unit_price = data.get("transaction_unit_price", line.transaction_unit_price)
                        line.tax_rate = data.get("tax_rate", line.tax_rate)
                        line.tax_code = data.get("tax_code", line.tax_code)
                    line.save()
                for receipt_line_id, old_line in existing.items():
                    if receipt_line_id not in supplied_ids:
                        old_line.delete()

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


class SupplierRefundSerializer(serializers.ModelSerializer):
    payment_account = serializers.PrimaryKeyRelatedField(queryset=ChartOfAccount.objects.all())
    supplier_name = serializers.CharField(source="supplier.name_ar", read_only=True)
    return_number = serializers.CharField(source="return_document.return_number", read_only=True)
    payment_account_name = serializers.CharField(source="payment_account.name_ar", read_only=True)
    journal_number = serializers.CharField(source="journal_entry.entry_number", read_only=True, allow_null=True)
    reversal_journal_number = serializers.CharField(source="reversal_journal_entry.entry_number", read_only=True, allow_null=True)

    class Meta:
        model = SupplierRefund
        fields = (
            "id", "company", "supplier", "supplier_name", "return_document", "return_number",
            "refund_number", "refund_date", "method", "payment_account", "payment_account_name",
            "amount", "reference", "notes", "status", "journal_entry", "journal_number",
            "reversal_journal_entry", "reversal_journal_number", "reversal_date", "reversed_at", "reversed_by",
            "created_at", "posted_at",
        )
        read_only_fields = ("status", "journal_entry", "journal_number", "reversal_journal_entry", "reversal_journal_number", "reversal_date", "reversed_at", "reversed_by", "created_at", "posted_at")

    def validate(self, attrs):
        company = attrs.get("company")
        supplier = attrs.get("supplier")
        returned = attrs.get("return_document")
        account = attrs.get("payment_account")
        if supplier and not supplier.is_active:
            raise serializers.ValidationError({"supplier": "المورد موقوف."})
        if not returned or returned.status != SupplierReturn.Status.POSTED:
            raise serializers.ValidationError({"return_document": "اختر مرتجع مورد مرحّل وغير معكوس."})
        if company and returned.company_id != company.pk:
            raise serializers.ValidationError({"return_document": "المرتجع لا يتبع الشركة المحددة."})
        if supplier and returned.supplier_id != supplier.pk:
            raise serializers.ValidationError({"supplier": "المورد لا يطابق مورد المرتجع."})
        refund_date = attrs.get("refund_date")
        if refund_date and refund_date < returned.return_date:
            raise serializers.ValidationError({"refund_date": "تاريخ الاسترداد لا يجوز أن يسبق تاريخ المرتجع."})
        if account:
            expected_kind = TreasuryAccount.Kind.CASH if attrs.get("method") == SupplierRefund.Method.CASH else TreasuryAccount.Kind.BANK
            treasury_account = TreasuryAccount.objects.filter(
                company=company, account=account, kind=expected_kind, is_active=True,
            ).first()
            branch = resolve_warehouse_branch(company, returned.invoice.receipt.warehouse)
            branch_id = branch.pk if branch else None
            if (
                account.company_id != company.pk or account.nature != ChartOfAccount.Nature.ASSET
                or not account.is_active or account.is_group or not account.allow_posting
                or not treasury_account
                or (treasury_account.branch_id and treasury_account.branch_id != branch_id)
                or treasury_account.currency_code != company.base_currency.code
            ):
                raise serializers.ValidationError({
                    "payment_account": "اختر حساب خزينة/بنك نشطًا تابعًا لفرع المرتجع (أو عامًا) وبعملة الشركة وبنفس طريقة الاسترداد."
                })
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        refund = SupplierRefund.objects.create(
            **validated_data, created_by=self.context["request"].user,
        )
        return refund


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
            except ValueError:
                raise serializers.ValidationError({"exchange_rate": "بيانات سعر الصرف غير صالحة أو لا تطابق السعر المعتمد."}) from None
            if base_amount != attrs.get("amount"):
                raise serializers.ValidationError({"amount": "المبلغ الأساسي لا يطابق المبلغ الأجنبي مضروبًا في سعر الصرف."})
        elif any(attrs.get(field) is not None for field in ("transaction_currency", "transaction_amount", "exchange_rate", "exchange_rate_date")):
            raise serializers.ValidationError({"transaction_currency": "لا تستخدم بيانات عملة أجنبية مع فاتورة بالعملة الأساسية."})
        account = attrs.get("payment_account")
        if account:
            expected_kind = TreasuryAccount.Kind.CASH if attrs.get("method") == SupplierPayment.Method.CASH else TreasuryAccount.Kind.BANK
            treasury_account = TreasuryAccount.objects.filter(company=company, account=account, kind=expected_kind, is_active=True).first()
            branch = invoice.branch or (resolve_warehouse_branch(company, invoice.receipt.warehouse) if invoice.receipt_id else None)
            branch_id = branch.pk if branch else None
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
    grand_total = serializers.SerializerMethodField()
    approval_status = serializers.SerializerMethodField()

    class Meta:
        model = PurchaseOrder
        fields = ("id", "company", "supplier", "supplier_name", "warehouse", "warehouse_name",
                  "order_number", "order_date", "expected_date", "status", "notes", "lines",
                  "line_items", "grand_total", "approval_status", "created_at", "confirmed_at")
        read_only_fields = ("status", "line_items", "grand_total", "approval_status", "created_at", "confirmed_at")

    def get_grand_total(self, obj):
        return str(obj.grand_total)

    def get_approval_status(self, obj):
        requests = list(obj.approval_requests.all())
        latest = max(requests, key=lambda item: (item.requested_at, item.pk), default=None)
        return latest.status if latest else None

    def get_line_items(self, obj):
        """تجهيز تفاصيل الأصناف المرتبطة بالمستند بصيغة مناسبة للواجهة."""
        return [{
            "id": line.pk, "product": line.product_id, "product_code": line.product.code,
            "product_name": line.product.name_ar, "product_type": line.product.product_type, "quantity": str(line.quantity),
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
            if not company or product.company_id != company.pk or product.product_type not in (Product.ProductType.GOODS, Product.ProductType.SERVICE) or not product.is_active:
                raise serializers.ValidationError({"lines": "كل البنود يجب أن تكون أصنافًا أو خدمات نشطة تابعة للشركة."})
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
    reversal_journal_number = serializers.CharField(source="reversal_journal_entry.entry_number", read_only=True, allow_null=True)
    reversal_movement_status = serializers.CharField(source="reversal_stock_movement.status", read_only=True, allow_null=True)
    transaction_currency_code = serializers.CharField(source="invoice.transaction_currency.code", read_only=True, allow_null=True)
    transaction_exchange_rate = serializers.DecimalField(source="invoice.exchange_rate", max_digits=20, decimal_places=10, read_only=True, allow_null=True)

    class Meta:
        model = SupplierReturn
        fields = (
            "id", "company", "supplier", "supplier_name", "invoice", "invoice_number",
            "return_number", "return_date", "status", "net_amount", "tax_amount",
            "grand_total", "transaction_currency_code", "transaction_exchange_rate",
            "transaction_net_amount", "transaction_tax_amount", "transaction_grand_total",
            "movement_status", "journal_entry", "journal_number", "reversal_date",
            "reversal_journal_entry", "reversal_journal_number", "reversal_movement_status",
            "notes", "lines", "return_lines", "created_at", "posted_at", "reversed_at",
        )
        read_only_fields = (
            "status", "net_amount", "tax_amount", "grand_total", "transaction_net_amount",
            "transaction_tax_amount", "transaction_grand_total", "transaction_currency_code",
            "transaction_exchange_rate", "movement_status",
            "journal_entry", "journal_number", "reversal_date", "reversal_journal_entry",
            "reversal_journal_number", "reversal_movement_status", "return_lines", "created_at", "posted_at", "reversed_at",
        )

    def validate(self, attrs):
        company = attrs.get("company")
        supplier = attrs.get("supplier")
        invoice = attrs.get("invoice")
        return_date = attrs.get("return_date")
        lines = attrs.get("lines", [])
        if not invoice or invoice.status != PurchaseInvoice.Status.POSTED:
            raise serializers.ValidationError({"invoice": "اختر فاتورة مورد مرحلة."})
        if invoice.invoice_type != PurchaseInvoice.InvoiceType.INVENTORY or not invoice.receipt_id:
            raise serializers.ValidationError({"invoice": "مرتجع المورد متاح لفواتير الأصناف المخزنية المرتبطة باستلام فقط."})
        if invoice.transaction_currency_id and (
            SupplierPayment.objects.filter(invoice=invoice, status=SupplierPayment.Status.POSTED).exists()
            or FxSubledgerRevaluation.objects.filter(purchase_invoice=invoice).filter(
                Q(journal_entry__isnull=True)
                | ~Q(journal_entry__status=JournalEntry.Status.REVERSED)
            ).exists()
        ):
            raise serializers.ValidationError({
                "invoice": "المرتجع الأجنبي متاح فقط إذا لم توجد دفعات أو إعادة تقييم سابقة على الفاتورة."
            })
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
            if returned + line["quantity"] > invoice_line.billed_quantity:
                raise serializers.ValidationError({
                    "lines": f"كمية المرتجع تتجاوز الكمية المفوترة المتبقية للصنف {invoice_line.receipt_line.product.code}."
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


class PurchaseRequisitionLineInputSerializer(serializers.Serializer):
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())
    requested_quantity = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0.0001"))
    estimated_unit_price = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0"), required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=250)


class PurchaseRequisitionSerializer(serializers.ModelSerializer):
    lines = PurchaseRequisitionLineInputSerializer(many=True, write_only=True)
    line_items = serializers.SerializerMethodField()
    requested_by_name = serializers.CharField(source="requested_by.get_full_name", read_only=True)
    reviewed_by_name = serializers.CharField(source="reviewed_by.get_full_name", read_only=True, allow_null=True)

    class Meta:
        from .models import PurchaseRequisition
        model = PurchaseRequisition
        fields = ("id", "company", "request_number", "request_date", "required_date", "department",
                  "purpose", "notes", "status", "requested_by", "requested_by_name",
                  "reviewed_by", "reviewed_by_name", "reviewed_at", "review_note",
                  "converted_order", "lines", "line_items", "created_at")
        read_only_fields = ("status", "requested_by", "reviewed_by", "reviewed_at",
                            "converted_order", "line_items", "created_at")

    def get_branch(self, obj):
        branch = resolve_warehouse_branch(obj.company, obj.order.warehouse)
        return branch.pk if branch else None

    def get_line_items(self, obj):
        return [{
            "id": line.pk, "product": line.product_id, "product_code": line.product.code,
            "product_name": line.product.name_ar, "product_type": line.product.product_type, "requested_quantity": str(line.requested_quantity),
            "estimated_unit_price": str(line.estimated_unit_price) if line.estimated_unit_price is not None else None,
            "notes": line.notes,
        } for line in obj.lines.select_related("product").all()]

    def validate(self, attrs):
        from .models import PurchaseRequisition
        company = attrs.get("company", self.instance.company if self.instance else None)
        lines = attrs.get("lines")
        if self.instance and self.instance.status not in (PurchaseRequisition.Status.DRAFT, PurchaseRequisition.Status.REJECTED):
            raise serializers.ValidationError("يمكن تعديل المسودة أو الطلب المرفوض فقط.")
        if self.instance and "company" in attrs and attrs["company"].pk != self.instance.company_id:
            raise serializers.ValidationError({"company": "لا يمكن نقل طلب الاحتياج إلى شركة أخرى بعد إنشائه."})
        if not self.instance and lines is None:
            raise serializers.ValidationError({"lines": "أضف صنفًا واحدًا على الأقل."})
        if lines is not None and not lines:
            raise serializers.ValidationError({"lines": "أضف صنفًا واحدًا على الأقل."})
        if lines is not None:
            for line in lines:
                product = line["product"]
                if not company or product.company_id != company.pk or product.product_type not in (Product.ProductType.GOODS, Product.ProductType.SERVICE) or not product.is_active:
                    raise serializers.ValidationError({"lines": "كل البنود يجب أن تكون أصنافًا أو خدمات نشطة تابعة للشركة."})
        if attrs.get("required_date") and attrs.get("request_date", self.instance.request_date if self.instance else None) and attrs["required_date"] < attrs.get("request_date", self.instance.request_date if self.instance else None):
            raise serializers.ValidationError({"required_date": "تاريخ الاحتياج لا يمكن أن يسبق تاريخ الطلب."})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        from .models import PurchaseRequisition, PurchaseRequisitionLine
        lines = validated_data.pop("lines")
        request = self.context["request"]
        requisition = PurchaseRequisition.objects.create(**validated_data, requested_by=request.user)
        PurchaseRequisitionLine.objects.bulk_create([
            PurchaseRequisitionLine(requisition=requisition, **line) for line in lines
        ])
        return requisition

    @transaction.atomic
    def update(self, instance, validated_data):
        from .models import PurchaseRequisition, PurchaseRequisitionLine
        requisition = PurchaseRequisition.objects.select_for_update().get(pk=instance.pk)
        if requisition.status not in (PurchaseRequisition.Status.DRAFT, PurchaseRequisition.Status.REJECTED):
            raise serializers.ValidationError("يمكن تعديل المسودة أو الطلب المرفوض فقط.")
        lines = validated_data.pop("lines", None)
        if requisition.status == PurchaseRequisition.Status.REJECTED:
            requisition.status = PurchaseRequisition.Status.DRAFT
            requisition.reviewed_by = None
            requisition.reviewed_at = None
            requisition.review_note = ""
        requisition = super().update(requisition, validated_data)
        if lines is not None:
            requisition.lines.all().delete()
            PurchaseRequisitionLine.objects.bulk_create([
                PurchaseRequisitionLine(requisition=requisition, **line) for line in lines
            ])
        return requisition



class PurchaseRFQLineInputSerializer(serializers.Serializer):
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())
    quantity = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0.0001"))
    notes = serializers.CharField(required=False, allow_blank=True, max_length=250)


class PurchaseRFQSerializer(serializers.ModelSerializer):
    lines = PurchaseRFQLineInputSerializer(many=True, write_only=True)
    invited_suppliers = serializers.PrimaryKeyRelatedField(queryset=Supplier.objects.filter(is_active=True), many=True, write_only=True)
    line_items = serializers.SerializerMethodField()
    supplier_list = serializers.SerializerMethodField()

    class Meta:
        model = PurchaseRFQ
        fields = ("id", "company", "source_requisition", "rfq_number", "issue_date", "closing_date", "status", "terms", "lines", "invited_suppliers", "line_items", "supplier_list", "awarded_quotation", "converted_order", "award_reason", "awarded_by", "awarded_at", "created_at")
        read_only_fields = ("status", "line_items", "supplier_list", "awarded_quotation", "converted_order", "award_reason", "awarded_by", "awarded_at", "created_at")

    def get_line_items(self, obj):
        return [{"id": x.pk, "product": x.product_id, "product_code": x.product.code, "product_name": x.product.name_ar, "product_type": x.product.product_type, "quantity": str(x.quantity), "notes": x.notes} for x in obj.lines.select_related("product")]

    def get_supplier_list(self, obj):
        return [{"id": x.supplier_id, "code": x.supplier.code, "name": x.supplier.name_ar, "status": x.status} for x in obj.invitations.select_related("supplier")]

    def validate(self, attrs):
        company = attrs["company"]
        lines, suppliers = attrs["lines"], attrs["invited_suppliers"]
        if not lines:
            raise serializers.ValidationError({"lines": "أضف صنفًا واحدًا على الأقل."})
        if not suppliers or len({x.pk for x in suppliers}) != len(suppliers):
            raise serializers.ValidationError({"invited_suppliers": "اختر موردين نشطين مختلفين."})
        if any(x.company_id != company.pk for x in suppliers):
            raise serializers.ValidationError({"invited_suppliers": "كل الموردين يجب أن يتبعوا الشركة."})
        product_ids = [x["product"].pk for x in lines]
        if len(product_ids) != len(set(product_ids)):
            raise serializers.ValidationError({"lines": "لا تكرر الصنف داخل طلب العروض؛ اجمع الكمية في بند واحد."})
        if any(x["product"].company_id != company.pk or x["product"].product_type not in (Product.ProductType.GOODS, Product.ProductType.SERVICE) or not x["product"].is_active for x in lines):
            raise serializers.ValidationError({"lines": "كل البنود يجب أن تكون أصنافًا أو خدمات نشطة من نفس الشركة."})
        source = attrs.get("source_requisition")
        if source:
            if source.company_id != company.pk or source.status != "approved":
                raise serializers.ValidationError({"source_requisition": "اختر طلب احتياج معتمدًا من نفس الشركة."})
            source_quantities = {}
            for source_line in source.lines.all():
                source_quantities[source_line.product_id] = (
                    source_quantities.get(source_line.product_id, Decimal("0"))
                    + source_line.requested_quantity
                )
            rfq_quantities = {line["product"].pk: line["quantity"] for line in lines}
            if source_quantities != rfq_quantities:
                raise serializers.ValidationError({
                    "lines": "أصناف وكميات طلب عروض الأسعار يجب أن تطابق طلب الاحتياج المرتبط بالكامل."
                })
        if attrs["closing_date"] < attrs["issue_date"]:
            raise serializers.ValidationError({"closing_date": "تاريخ الإغلاق لا يسبق تاريخ الإصدار."})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        lines = validated_data.pop("lines")
        suppliers = validated_data.pop("invited_suppliers")
        rfq = PurchaseRFQ.objects.create(**validated_data, created_by=self.context["request"].user)
        PurchaseRFQLine.objects.bulk_create([PurchaseRFQLine(rfq=rfq, **line) for line in lines])
        PurchaseRFQInvitation.objects.bulk_create([PurchaseRFQInvitation(rfq=rfq, supplier=s) for s in suppliers])
        return rfq


class SupplierQuotationLineInputSerializer(serializers.Serializer):
    rfq_line = serializers.PrimaryKeyRelatedField(queryset=PurchaseRFQLine.objects.all())
    unit_price = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0"))
    discount_percent = serializers.DecimalField(max_digits=7, decimal_places=4, min_value=Decimal("0"), max_value=Decimal("100"), required=False, default=Decimal("0"))
    tax_rate = serializers.DecimalField(max_digits=7, decimal_places=4, min_value=Decimal("0"), max_value=Decimal("100"), required=False, default=Decimal("0"))


class SupplierQuotationSerializer(serializers.ModelSerializer):
    lines = SupplierQuotationLineInputSerializer(many=True, write_only=True)
    line_items = serializers.SerializerMethodField()
    supplier_name = serializers.CharField(source="supplier.name_ar", read_only=True)
    total_before_tax = serializers.SerializerMethodField()
    tax_total = serializers.SerializerMethodField()
    grand_total = serializers.SerializerMethodField()

    class Meta:
        model = SupplierQuotation
        fields = ("id", "rfq", "supplier", "supplier_name", "quotation_number", "quotation_date", "valid_until", "delivery_days", "payment_terms", "notes", "status", "lines", "line_items", "total_before_tax", "tax_total", "grand_total", "submitted_at")
        read_only_fields = ("status", "line_items", "total_before_tax", "tax_total", "grand_total", "submitted_at")

    def _totals(self, obj):
        net = sum((x.net_amount for x in obj.lines.all()), Decimal("0"))
        tax = sum((x.tax_amount for x in obj.lines.all()), Decimal("0"))
        return net, tax, net + tax

    def get_line_items(self, obj):
        return [{"id": x.pk, "rfq_line": x.rfq_line_id, "product_code": x.rfq_line.product.code, "product_name": x.rfq_line.product.name_ar, "product_type": x.rfq_line.product.product_type, "quantity": str(x.rfq_line.quantity), "unit_price": str(x.unit_price), "discount_percent": str(x.discount_percent), "tax_rate": str(x.tax_rate), "net_amount": str(x.net_amount), "tax_amount": str(x.tax_amount), "total_amount": str(x.total_amount)} for x in obj.lines.select_related("rfq_line__product")]

    def get_total_before_tax(self, obj):
        return str(self._totals(obj)[0])

    def get_tax_total(self, obj):
        return str(self._totals(obj)[1])

    def get_grand_total(self, obj):
        return str(self._totals(obj)[2])

    def validate(self, attrs):
        rfq, supplier, lines = attrs["rfq"], attrs["supplier"], attrs["lines"]
        if rfq.status != PurchaseRFQ.Status.OPEN:
            raise serializers.ValidationError({"rfq": "طلب العروض غير مفتوح لاستقبال عروض."})
        if attrs["quotation_date"] < rfq.issue_date or attrs["quotation_date"] > rfq.closing_date:
            raise serializers.ValidationError({"quotation_date": "تاريخ العرض يجب أن يقع بين إصدار طلب العروض وموعد إغلاقه."})
        if attrs["valid_until"] < attrs["quotation_date"]:
            raise serializers.ValidationError({"valid_until": "صلاحية العرض لا تسبق تاريخ العرض."})
        invitation = rfq.invitations.filter(supplier=supplier).first()
        if supplier.company_id != rfq.company_id or not invitation:
            raise serializers.ValidationError({"supplier": "المورد غير مدعو لهذا الطلب."})
        if invitation.status != PurchaseRFQInvitation.Status.INVITED or SupplierQuotation.objects.filter(rfq=rfq, supplier=supplier).exists():
            raise serializers.ValidationError({"supplier": "تم تسجيل عرض لهذا المورد بالفعل أو لم يعد مدعوًا."})
        if SupplierQuotation.objects.filter(supplier=supplier, quotation_number=attrs["quotation_number"]).exists():
            raise serializers.ValidationError({"quotation_number": "رقم العرض مستخدم من قبل لهذا المورد."})
        required = set(rfq.lines.values_list("id", flat=True))
        provided = [x["rfq_line"].pk for x in lines]
        if set(provided) != required or len(provided) != len(required):
            raise serializers.ValidationError({"lines": "يجب تسعير كل البنود مرة واحدة دون تكرار."})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        lines = validated_data.pop("lines")
        quotation = SupplierQuotation.objects.create(**validated_data, submitted_by=self.context["request"].user)
        SupplierQuotationLine.objects.bulk_create([SupplierQuotationLine(quotation=quotation, **line) for line in lines])
        PurchaseRFQInvitation.objects.filter(rfq=quotation.rfq, supplier=quotation.supplier).update(status=PurchaseRFQInvitation.Status.QUOTED)
        return quotation


class PurchaseServiceAcceptanceLineInputSerializer(serializers.Serializer):
    order_line = serializers.PrimaryKeyRelatedField(queryset=PurchaseOrderLine.objects.select_related("order", "product"))
    quantity = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0.0001"))
    notes = serializers.CharField(required=False, allow_blank=True, max_length=250)


class PurchaseServiceAcceptanceSerializer(serializers.ModelSerializer):
    lines = PurchaseServiceAcceptanceLineInputSerializer(many=True, write_only=True)
    line_items = serializers.SerializerMethodField()
    supplier_name = serializers.CharField(source="supplier.name_ar", read_only=True)
    order_number = serializers.CharField(source="order.order_number", read_only=True)
    branch = serializers.SerializerMethodField()
    accepted_by_name = serializers.CharField(source="accepted_by.get_full_name", read_only=True, allow_null=True)

    class Meta:
        model = PurchaseServiceAcceptance
        fields = (
            "id", "company", "order", "order_number", "supplier", "supplier_name", "branch",
            "acceptance_number", "acceptance_date", "status", "notes", "accepted_by",
            "accepted_by_name", "accepted_at", "lines", "line_items", "created_at",
        )
        read_only_fields = ("status", "accepted_by", "accepted_by_name", "accepted_at", "line_items", "created_at")

    def get_branch(self, obj):
        branch = resolve_warehouse_branch(obj.company, obj.order.warehouse)
        return branch.pk if branch else None

    def get_line_items(self, obj):
        return [{
            "id": line.pk,
            "order_line": line.order_line_id,
            "product": line.order_line.product_id,
            "product_code": line.order_line.product.code,
            "product_name": line.order_line.product.name_ar,
            "quantity": str(line.quantity),
            "notes": line.notes,
        } for line in obj.lines.select_related("order_line__product")]

    def validate(self, attrs):
        company = attrs["company"]
        order = attrs["order"]
        supplier = attrs["supplier"]
        lines = attrs["lines"]
        if order.company_id != company.pk or order.supplier_id != supplier.pk:
            raise serializers.ValidationError({"order": "أمر الشراء والمورد يجب أن يتبعا الشركة المحددة."})
        if order.status not in (PurchaseOrder.Status.CONFIRMED, PurchaseOrder.Status.PARTIALLY_RECEIVED, PurchaseOrder.Status.RECEIVED):
            raise serializers.ValidationError({"order": "لا يمكن استلام خدمة قبل اعتماد أمر الشراء."})
        if not lines:
            raise serializers.ValidationError({"lines": "أضف بند خدمة واحدًا على الأقل."})
        seen = set()
        requested_by_line = {}
        for line in lines:
            order_line = line["order_line"]
            if order_line.order_id != order.pk or order_line.product.product_type != Product.ProductType.SERVICE:
                raise serializers.ValidationError({"lines": "محضر الخدمة يقبل بنود الخدمات التابعة لأمر الشراء فقط."})
            if order_line.pk in seen:
                raise serializers.ValidationError({"lines": "لا تكرر بند الخدمة داخل المحضر."})
            seen.add(order_line.pk)
            requested_by_line[order_line.pk] = line["quantity"]
        for line_id, quantity in requested_by_line.items():
            accepted = PurchaseServiceAcceptanceLine.objects.filter(
                order_line_id=line_id,
                acceptance__status=PurchaseServiceAcceptance.Status.ACCEPTED,
            ).aggregate(total=Sum("quantity"))["total"] or Decimal("0")
            if accepted + quantity > PurchaseOrderLine.objects.get(pk=line_id).quantity:
                raise serializers.ValidationError({"lines": "كمية الخدمة تتجاوز الكمية المتبقية في أمر الشراء."})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        lines = validated_data.pop("lines")
        document = PurchaseServiceAcceptance.objects.create(**validated_data)
        for line in lines:
            item = PurchaseServiceAcceptanceLine(acceptance=document, **line)
            item.full_clean()
            item.save()
        return document



class LandedCostAllocationInputSerializer(serializers.Serializer):
    source_invoice_line = serializers.PrimaryKeyRelatedField(queryset=PurchaseInvoiceLine.objects.select_related("invoice"))
    receipt_line = serializers.PrimaryKeyRelatedField(
        queryset=StockMovementLine.objects.select_related("movement", "product"),
        required=False,
    )
    receipt_lines = serializers.ListField(
        child=serializers.PrimaryKeyRelatedField(
            queryset=StockMovementLine.objects.select_related("movement", "product"),
        ),
        required=False,
        allow_empty=False,
    )
    basis = serializers.ChoiceField(choices=LandedCostAllocation.Basis.choices, default=LandedCostAllocation.Basis.MANUAL)
    # For automatic bases, amount is the total expense amount to distribute over receipt_lines.
    amount = serializers.DecimalField(max_digits=20, decimal_places=4, min_value=Decimal("0.0001"))


class LandedCostDocumentSerializer(serializers.ModelSerializer):
    allocations = LandedCostAllocationInputSerializer(many=True, write_only=True)
    allocation_lines = serializers.SerializerMethodField()
    source_invoice_number = serializers.CharField(source="source_invoice.invoice_number", read_only=True)
    supplier_name = serializers.CharField(source="source_invoice.supplier.name_ar", read_only=True)

    class Meta:
        model = LandedCostDocument
        fields = (
            "id", "company", "source_invoice", "source_invoice_number", "supplier_name",
            "document_number", "document_date", "status", "total_amount", "notes",
            "allocations", "allocation_lines", "created_at", "posted_at", "reversed_at",
        )
        read_only_fields = ("status", "total_amount", "allocation_lines", "created_at", "posted_at", "reversed_at")

    def get_allocation_lines(self, obj):
        return [{
            "id": line.pk,
            "source_invoice_line": line.source_invoice_line_id,
            "source_description": line.source_invoice_line.description,
            "expense_account": line.source_invoice_line.expense_account_id,
            "receipt_line": line.receipt_line_id,
            "product": line.receipt_line.product_id,
            "product_code": line.receipt_line.product.code,
            "product_name": line.receipt_line.product.name_ar,
            "quantity": str(line.receipt_line.quantity),
            "basis": line.basis,
            "amount": str(line.amount),
            "distributions": [{
                "branch": dist.branch_id,
                "branch_name": dist.branch.name_ar,
                "inventory_amount": str(dist.inventory_amount),
                "cogs_amount": str(dist.cogs_amount),
                "adjustment_loss_amount": str(dist.adjustment_loss_amount),
                "journal_entry": dist.journal_entry_id,
            } for dist in line.distributions.select_related("branch").all()],
        } for line in obj.allocations.select_related(
            "source_invoice_line__expense_account", "receipt_line__product",
        ).prefetch_related("distributions__branch").all()]

    def validate(self, attrs):
        company = attrs["company"]
        invoice = attrs["source_invoice"]
        document_date = attrs["document_date"]
        input_rows = attrs.get("allocations") or []
        if invoice.company_id != company.pk:
            raise serializers.ValidationError({"source_invoice": "فاتورة المصدر لا تتبع الشركة المحددة."})
        if invoice.invoice_type != PurchaseInvoice.InvoiceType.DIRECT_EXPENSE or invoice.status != PurchaseInvoice.Status.POSTED:
            raise serializers.ValidationError({"source_invoice": "اختر فاتورة خدمات أو مصروفات مباشرة ومرحّلة."})
        if document_date < invoice.invoice_date:
            raise serializers.ValidationError({"document_date": "تاريخ الرسملة لا يجوز أن يسبق فاتورة المصروف."})
        if not input_rows:
            raise serializers.ValidationError({"allocations": "أضف بند رسملة واحدًا على الأقل."})

        expanded_rows = []
        requested_by_source = {}
        quantum = Decimal("0.0001")
        for row in input_rows:
            source_line = row["source_invoice_line"]
            basis = row["basis"]
            amount = row["amount"]
            if source_line.invoice_id != invoice.pk or not source_line.expense_account_id:
                raise serializers.ValidationError({"allocations": "كل بند مصدر يجب أن يكون بند مصروف تابعًا لفاتورة المصدر."})
            account = source_line.expense_account
            if (
                not account.is_active or account.is_group or not account.allow_posting
                or account.nature != ChartOfAccount.Nature.EXPENSE
            ):
                raise serializers.ValidationError({"allocations": "حساب بند المصروف يجب أن يكون حساب مصروف تفصيليًا نشطًا ويسمح بالترحيل."})

            if basis == LandedCostAllocation.Basis.MANUAL:
                receipt_lines = [row.get("receipt_line")]
                if not receipt_lines[0] or row.get("receipt_lines"):
                    raise serializers.ValidationError({"allocations": "التوزيع اليدوي يتطلب بند استلام واحدًا لكل مبلغ."})
                shares = [amount]
            else:
                if basis not in (
                    LandedCostAllocation.Basis.VALUE, LandedCostAllocation.Basis.QUANTITY,
                    LandedCostAllocation.Basis.WEIGHT, LandedCostAllocation.Basis.VOLUME,
                ):
                    raise serializers.ValidationError({"allocations": "أساس التوزيع غير مدعوم."})
                receipt_lines = row.get("receipt_lines") or []
                if row.get("receipt_line") or not receipt_lines:
                    raise serializers.ValidationError({"allocations": "التوزيع الآلي يتطلب اختيار عدة بنود استلام في حقل receipt_lines."})
                if len({line.pk for line in receipt_lines}) != len(receipt_lines):
                    raise serializers.ValidationError({"allocations": "لا تكرر بند الاستلام داخل نفس عملية التوزيع."})
                weights = []
                for receipt in receipt_lines:
                    if basis == LandedCostAllocation.Basis.QUANTITY:
                        weight = receipt.quantity
                    elif basis == LandedCostAllocation.Basis.VALUE:
                        weight = receipt.cost_amount
                    elif basis == LandedCostAllocation.Basis.WEIGHT:
                        weight = receipt.quantity * receipt.product.unit_weight
                    else:
                        weight = receipt.quantity * receipt.product.unit_volume
                    if weight is None or weight <= 0:
                        basis_label = {
                            LandedCostAllocation.Basis.VALUE: "قيمة الاستلام",
                            LandedCostAllocation.Basis.QUANTITY: "كمية الاستلام",
                            LandedCostAllocation.Basis.WEIGHT: "وزن الصنف",
                            LandedCostAllocation.Basis.VOLUME: "حجم الصنف",
                        }[basis]
                        raise serializers.ValidationError({
                            "allocations": f"لا يمكن التوزيع على {receipt.product.code}؛ أدخل بيانات {basis_label} موجبة في بطاقة الصنف."
                        })
                    weights.append(Decimal(weight))
                weight_total = sum(weights, Decimal("0"))
                shares = [(amount * weight / weight_total).quantize(quantum) for weight in weights]
                residual = amount - sum(shares, Decimal("0"))
                if residual:
                    largest = max(range(len(weights)), key=lambda index: weights[index])
                    shares[largest] += residual
                if sum(shares, Decimal("0")) != amount:
                    raise serializers.ValidationError({"allocations": "تعذر توزيع المبلغ بدقة؛ تحقق من قيمة المصروف وأوزان بنود الاستلام."})

            for receipt_line, share in zip(receipt_lines, shares):
                if share <= 0:
                    continue
                if receipt_line.movement.company_id != company.pk or receipt_line.movement.status != StockMovement.Status.POSTED:
                    raise serializers.ValidationError({"allocations": "كل بند استلام يجب أن يكون مرحّلًا ويتبع الشركة."})
                if receipt_line.product.product_type != Product.ProductType.GOODS:
                    raise serializers.ValidationError({"allocations": "رسملة التكلفة متاحة للأصناف المخزنية فقط."})
                if not PurchaseReceipt.objects.filter(company=company, stock_movement_id=receipt_line.movement_id).exists():
                    raise serializers.ValidationError({"allocations": "بند المخزون المحدد ليس جزءًا من إذن استلام مشتريات."})
                expanded_rows.append({
                    "source_invoice_line": source_line,
                    "receipt_line": receipt_line,
                    "basis": basis,
                    "amount": share,
                })
                requested_by_source[source_line.pk] = requested_by_source.get(source_line.pk, Decimal("0")) + share

        seen_pairs = set()
        for expanded in expanded_rows:
            pair = (expanded["source_invoice_line"].pk, expanded["receipt_line"].pk)
            if pair in seen_pairs:
                raise serializers.ValidationError({"allocations": "لا يمكن تكرار نفس بند المصروف مع نفس بند الاستلام في المستند."})
            seen_pairs.add(pair)

        for source_id, requested in requested_by_source.items():
            source_line = PurchaseInvoiceLine.objects.select_related("invoice").get(pk=source_id)
            nonrecoverable_tax = (
                source_line.tax_amount
                if source_line.tax_code_id and not source_line.tax_code.input_tax_recoverable
                else Decimal("0")
            )
            capitalizable_amount = source_line.net_amount + nonrecoverable_tax
            already_allocated = LandedCostAllocation.objects.filter(
                source_invoice_line_id=source_id,
            ).exclude(document__status=LandedCostDocument.Status.REVERSED).aggregate(
                total=Sum("amount"),
            )["total"] or Decimal("0")
            if already_allocated + requested > capitalizable_amount:
                raise serializers.ValidationError({
                    "allocations": f"إجمالي الرسملة يتجاوز صافي بند المصروف {source_line.description or source_line.pk}."
                })
        attrs["allocations"] = expanded_rows
        attrs["computed_total"] = sum((row["amount"] for row in expanded_rows), Decimal("0"))
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        allocations = validated_data.pop("allocations")
        total = validated_data.pop("computed_total")
        invoice = PurchaseInvoice.objects.select_for_update(of=("self",)).get(
            pk=validated_data["source_invoice"].pk,
        )
        if invoice.status != PurchaseInvoice.Status.POSTED or invoice.invoice_type != PurchaseInvoice.InvoiceType.DIRECT_EXPENSE:
            raise serializers.ValidationError({"source_invoice": "فاتورة المصدر لم تعد مؤهلة للرسملة."})
        grouped = {}
        for row in allocations:
            grouped[row["source_invoice_line"].pk] = grouped.get(row["source_invoice_line"].pk, Decimal("0")) + row["amount"]
        for source_id, requested in grouped.items():
            source_line = PurchaseInvoiceLine.objects.select_for_update(of=("self",)).get(pk=source_id)
            nonrecoverable_tax = (
                source_line.tax_amount
                if source_line.tax_code_id and not source_line.tax_code.input_tax_recoverable
                else Decimal("0")
            )
            capitalizable_amount = source_line.net_amount + nonrecoverable_tax
            already_allocated = LandedCostAllocation.objects.filter(
                source_invoice_line_id=source_id,
            ).exclude(document__status=LandedCostDocument.Status.REVERSED).aggregate(
                total=Sum("amount"),
            )["total"] or Decimal("0")
            if already_allocated + requested > capitalizable_amount:
                raise serializers.ValidationError({"allocations": "تم استهلاك رصيد بند المصروف في رسملة أخرى؛ حدّث البيانات."})
        document = LandedCostDocument.objects.create(**validated_data, total_amount=total)
        for row in allocations:
            allocation = LandedCostAllocation(document=document, **row)
            allocation.full_clean()
            allocation.save()
        return document

    def update(self, instance, validated_data):
        raise serializers.ValidationError("لا يمكن تعديل مستند الرسملة بعد إنشائه؛ احذف المسودة وأعد إنشاءها.")


class BranchPurchasePolicySerializer(serializers.ModelSerializer):
    branch = serializers.PrimaryKeyRelatedField(queryset=Branch.objects.all())
    branch_id = serializers.IntegerField(source="branch.id", read_only=True)
    branch_name = serializers.CharField(source="branch.name_ar", read_only=True)
    allowed_supplier_ids = serializers.PrimaryKeyRelatedField(
        source="allowed_suppliers", many=True, required=False,
        queryset=Supplier.objects.all(),
    )

    class Meta:
        from .models import BranchPurchasePolicy
        model = BranchPurchasePolicy
        fields = (
            "id", "branch", "branch_id", "branch_name", "purchase_enabled",
            "restrict_suppliers", "allowed_supplier_ids", "updated_at",
        )
        read_only_fields = ("id", "branch_id", "branch_name", "updated_at")

    def validate(self, attrs):
        branch = attrs.get("branch", getattr(self.instance, "branch", None))
        if self.instance and "branch" in attrs and attrs["branch"].pk != self.instance.branch_id:
            raise serializers.ValidationError({"branch": "لا يمكن نقل سياسة شراء قائمة إلى فرع آخر."})
        suppliers = attrs.get("allowed_suppliers")
        if branch and suppliers is not None:
            invalid = [supplier.pk for supplier in suppliers if supplier.company_id != branch.company_id]
            if invalid:
                raise serializers.ValidationError({
                    "allowed_supplier_ids": "كل الموردين المحددين يجب أن يتبعوا نفس شركة الفرع."
                })
        return attrs
