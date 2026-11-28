from decimal import Decimal

from django.db import transaction
from rest_framework import serializers

from inventory.models import StockMovement, StockMovementLine
from .models import SalesInvoice, SalesInvoiceLine, SalesReturn, SalesReturnLine


class SalesReturnLineInputSerializer(serializers.Serializer):
    invoice_line = serializers.PrimaryKeyRelatedField(queryset=SalesInvoiceLine.objects.select_related("invoice", "delivery_line", "product"))
    quantity = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0.0001"))


class SalesReturnLineSerializer(serializers.ModelSerializer):
    product_code = serializers.CharField(source="invoice_line.product.code", read_only=True)
    product_name = serializers.CharField(source="invoice_line.product.name_ar", read_only=True)

    class Meta:
        model = SalesReturnLine
        fields = ("id", "invoice_line", "product_code", "product_name", "quantity",
                  "net_amount", "tax_amount", "total_amount", "transaction_net_amount",
                  "transaction_tax_amount", "transaction_total_amount", "cost_amount")
        read_only_fields = fields


class SalesReturnSerializer(serializers.ModelSerializer):
    lines = SalesReturnLineInputSerializer(many=True, write_only=True)
    line_items = SalesReturnLineSerializer(source="lines", many=True, read_only=True)
    invoice_number = serializers.CharField(source="invoice.invoice_number", read_only=True)
    customer_name = serializers.CharField(source="invoice.customer.name_ar", read_only=True)
    transaction_currency_code = serializers.CharField(source="invoice.transaction_currency.code", read_only=True, allow_null=True)
    movement_status = serializers.CharField(source="stock_movement.status", read_only=True, allow_null=True)
    journal_number = serializers.CharField(source="journal_entry.entry_number", read_only=True, allow_null=True)
    cost_journal_number = serializers.CharField(source="cost_journal_entry.entry_number", read_only=True, allow_null=True)
    reversal_journal_number = serializers.CharField(source="reversal_journal_entry.entry_number", read_only=True, allow_null=True)

    class Meta:
        model = SalesReturn
        fields = ("id", "company", "invoice", "invoice_number", "customer_name", "warehouse", "branch",
                  "return_number", "return_date", "status", "net_total", "tax_total", "grand_total",
                  "transaction_currency_code", "transaction_net_total", "transaction_tax_total", "transaction_grand_total",
                  "movement_status", "journal_entry", "journal_number", "cost_journal_entry",
                  "cost_journal_number", "reversal_stock_movement", "reversal_journal_entry",
                  "reversal_journal_number", "reversal_date", "reversed_at", "reversed_by",
                  "notes", "lines", "line_items", "print_template_snapshot", "created_at", "posted_at")
        read_only_fields = ("warehouse", "branch", "status", "net_total", "tax_total", "grand_total",
                            "transaction_net_total", "transaction_tax_total", "transaction_grand_total",
                            "movement_status", "journal_entry", "journal_number", "cost_journal_entry",
                            "cost_journal_number", "reversal_stock_movement", "reversal_journal_entry",
                            "reversal_journal_number", "reversal_date", "reversed_at", "reversed_by",
                            "line_items", "print_template_snapshot", "created_at", "posted_at")

    def validate(self, attrs):
        invoice = attrs.get("invoice", self.instance.invoice if self.instance else None)
        company = attrs.get("company", self.instance.company if self.instance else None)
        return_date = attrs.get("return_date", self.instance.return_date if self.instance else None)
        if return_date and invoice and return_date < invoice.invoice_date:
            raise serializers.ValidationError({"return_date": "تاريخ المردود لا يجوز أن يسبق تاريخ الفاتورة الأصلية."})
        if self.instance:
            if self.instance.status != SalesReturn.Status.DRAFT:
                raise serializers.ValidationError("لا يمكن تعديل مردود مبيعات مرحّل.")
            if company.pk != self.instance.company_id or invoice.pk != self.instance.invoice_id:
                raise serializers.ValidationError("لا يمكن تغيير الشركة أو الفاتورة الأصلية بعد إنشاء المردود.")
            if "lines" not in attrs:
                return attrs
        if not invoice or invoice.status != SalesInvoice.Status.POSTED:
            raise serializers.ValidationError({"invoice": "اختر فاتورة مبيعات مرحلة."})
        if invoice.company_id != company.pk:
            raise serializers.ValidationError({"invoice": "الفاتورة لا تتبع الشركة المحددة."})
        if invoice.delivery_id and not invoice.delivery.warehouse.is_active:
            raise serializers.ValidationError({"invoice": "مخزن الفاتورة الأصلية موقوف."})
        if not invoice.delivery_id and (not invoice.branch_id or not invoice.branch.is_active):
            raise serializers.ValidationError({"invoice": "فرع فاتورة الخدمة موقوف أو غير محدد."})
        lines = attrs.get("lines", [])
        if not lines:
            raise serializers.ValidationError({"lines": "أضف صنفًا واحدًا على الأقل."})
        ids = [row["invoice_line"].pk for row in lines]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError({"lines": "لا تكرر بند الفاتورة في نفس المردود."})
        for row in lines:
            invoice_line = row["invoice_line"]
            if invoice_line.invoice_id != invoice.pk:
                raise serializers.ValidationError({"lines": "كل البنود يجب أن تكون من الفاتورة المحددة."})
            quantum = Decimal("1").scaleb(-invoice_line.product.unit.decimal_places)
            if row["quantity"] != row["quantity"].quantize(quantum):
                raise serializers.ValidationError({
                    "lines": f"كمية المرتجع للصنف {invoice_line.product.code} تتجاوز دقة وحدة القياس ({invoice_line.product.unit.decimal_places} منازل عشرية)."
                })
            if invoice.transaction_currency_id and (
                row["invoice_line"].transaction_net_amount is None
                or row["invoice_line"].transaction_tax_amount is None
            ):
                raise serializers.ValidationError({"lines": "لقطة العملة الأجنبية غير مكتملة في بند الفاتورة الأصلية."})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        lines_data = validated_data.pop("lines")
        invoice = validated_data["invoice"]
        company = validated_data["company"]
        warehouse = invoice.delivery.warehouse if invoice.delivery_id else None
        return_number = validated_data["return_number"]
        movement = None
        if invoice.delivery_id:
            warehouse = invoice.delivery.warehouse
            movement = StockMovement.objects.create(
                company=company, movement_number=f"SR-{return_number}",
                movement_type=StockMovement.MovementType.RECEIPT,
                movement_date=validated_data["return_date"],
                destination_warehouse=warehouse,
                notes=validated_data.get("notes", ""), created_by=self.context["request"].user,
            )
        else:
            warehouse = None
        sales_return = SalesReturn.objects.create(
            **validated_data, warehouse=warehouse, branch=invoice.branch if not movement else None,
            stock_movement=movement, created_by=self.context["request"].user,
        )
        for row in lines_data:
            invoice_line = row["invoice_line"]
            quantity = row["quantity"]
            source = invoice_line.delivery_line
            movement_line = None
            if source:
                unit_cost = (source.cost_amount / source.quantity).quantize(Decimal("0.000001"))
                movement_line = StockMovementLine.objects.create(
                    movement=movement, product=source.product, quantity=quantity,
                    unit_cost=unit_cost, destination_location=source.source_location,
                    expiry_date=source.expiry_date,
                )
            original_qty = invoice_line.quantity
            net = (invoice_line.net_amount * quantity / original_qty).quantize(Decimal("0.0001"))
            tax = (invoice_line.tax_amount * quantity / original_qty).quantize(Decimal("0.0001"))
            transaction_net = (
                (invoice_line.transaction_net_amount * quantity / original_qty).quantize(Decimal("0.0001"))
                if invoice.transaction_currency_id else None
            )
            transaction_tax = (
                (invoice_line.transaction_tax_amount * quantity / original_qty).quantize(Decimal("0.0001"))
                if invoice.transaction_currency_id else None
            )
            cost = (source.cost_amount * quantity / source.quantity).quantize(Decimal("0.0001")) if source else Decimal("0")
            SalesReturnLine.objects.create(
                sales_return=sales_return, invoice_line=invoice_line,
                movement_line=movement_line, quantity=quantity,
                net_amount=net, tax_amount=tax, total_amount=net + tax,
                transaction_net_amount=transaction_net,
                transaction_tax_amount=transaction_tax,
                transaction_total_amount=(transaction_net + transaction_tax) if transaction_net is not None else None,
                cost_amount=cost,
            )
        return sales_return

    @transaction.atomic
    def update(self, instance, validated_data):
        sales_return = SalesReturn.objects.select_for_update().get(pk=instance.pk)
        if sales_return.status != SalesReturn.Status.DRAFT:
            raise serializers.ValidationError("لا يمكن تعديل مردود مبيعات مرحّل.")
        lines_data = validated_data.pop("lines", None)
        movement = StockMovement.objects.select_for_update().get(pk=sales_return.stock_movement_id) if sales_return.stock_movement_id else None
        if lines_data is not None:
            sales_return.lines.all().delete()
            if movement:
                movement.lines.all().delete()
            for row in lines_data:
                invoice_line = row["invoice_line"]
                source = invoice_line.delivery_line
                quantity = row["quantity"]
                movement_line = None
                if source:
                    unit_cost = (source.cost_amount / source.quantity).quantize(Decimal("0.000001"))
                    movement_line = StockMovementLine.objects.create(
                        movement=movement, product=source.product, quantity=quantity,
                        unit_cost=unit_cost, destination_location=source.source_location,
                        expiry_date=source.expiry_date,
                    )
                original_qty = invoice_line.quantity
                net = (invoice_line.net_amount * quantity / original_qty).quantize(Decimal("0.0001"))
                tax = (invoice_line.tax_amount * quantity / original_qty).quantize(Decimal("0.0001"))
                transaction_net = (
                    (invoice_line.transaction_net_amount * quantity / original_qty).quantize(Decimal("0.0001"))
                    if sales_return.invoice.transaction_currency_id else None
                )
                transaction_tax = (
                    (invoice_line.transaction_tax_amount * quantity / original_qty).quantize(Decimal("0.0001"))
                    if sales_return.invoice.transaction_currency_id else None
                )
                cost = (source.cost_amount * quantity / source.quantity).quantize(Decimal("0.0001")) if source else Decimal("0")
                SalesReturnLine.objects.create(
                    sales_return=sales_return, invoice_line=invoice_line,
                    movement_line=movement_line, quantity=quantity,
                    net_amount=net, tax_amount=tax, total_amount=net + tax,
                    transaction_net_amount=transaction_net,
                    transaction_tax_amount=transaction_tax,
                    transaction_total_amount=(transaction_net + transaction_tax) if transaction_net is not None else None,
                    cost_amount=cost,
                )
        for field, value in validated_data.items():
            setattr(sales_return, field, value)
        sales_return.save()
        if movement:
            movement.movement_date = sales_return.return_date
            movement.notes = sales_return.notes
            movement.save(update_fields=("movement_date", "notes"))
        return sales_return
