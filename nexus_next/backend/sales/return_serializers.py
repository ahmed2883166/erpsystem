from decimal import Decimal

from django.db import transaction
from rest_framework import serializers

from inventory.models import StockMovement, StockMovementLine
from .models import SalesInvoice, SalesInvoiceLine, SalesReturn, SalesReturnLine


class SalesReturnLineInputSerializer(serializers.Serializer):
    invoice_line = serializers.PrimaryKeyRelatedField(queryset=SalesInvoiceLine.objects.select_related("invoice", "delivery_line", "delivery_line__product"))
    quantity = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0.0001"))


class SalesReturnLineSerializer(serializers.ModelSerializer):
    product_code = serializers.CharField(source="invoice_line.delivery_line.product.code", read_only=True)
    product_name = serializers.CharField(source="invoice_line.delivery_line.product.name_ar", read_only=True)

    class Meta:
        model = SalesReturnLine
        fields = ("id", "invoice_line", "product_code", "product_name", "quantity",
                  "net_amount", "tax_amount", "total_amount", "cost_amount")
        read_only_fields = fields


class SalesReturnSerializer(serializers.ModelSerializer):
    lines = SalesReturnLineInputSerializer(many=True, write_only=True)
    line_items = SalesReturnLineSerializer(source="lines", many=True, read_only=True)
    invoice_number = serializers.CharField(source="invoice.invoice_number", read_only=True)
    customer_name = serializers.CharField(source="invoice.customer.name_ar", read_only=True)
    movement_status = serializers.CharField(source="stock_movement.status", read_only=True)
    journal_number = serializers.CharField(source="journal_entry.entry_number", read_only=True, allow_null=True)
    cost_journal_number = serializers.CharField(source="cost_journal_entry.entry_number", read_only=True, allow_null=True)

    class Meta:
        model = SalesReturn
        fields = ("id", "company", "invoice", "invoice_number", "customer_name", "warehouse",
                  "return_number", "return_date", "status", "net_total", "tax_total", "grand_total",
                  "movement_status", "journal_entry", "journal_number", "cost_journal_entry",
                  "cost_journal_number", "notes", "lines", "line_items", "created_at", "posted_at")
        read_only_fields = ("warehouse", "status", "net_total", "tax_total", "grand_total",
                            "movement_status", "journal_entry", "journal_number", "cost_journal_entry",
                            "cost_journal_number", "line_items", "created_at", "posted_at")

    def validate(self, attrs):
        invoice = attrs.get("invoice", self.instance.invoice if self.instance else None)
        company = attrs.get("company", self.instance.company if self.instance else None)
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
        return_date = attrs.get("return_date", self.instance.return_date if self.instance else None)
        if return_date and return_date < invoice.invoice_date:
            raise serializers.ValidationError({"return_date": "تاريخ المردود لا يجوز أن يسبق تاريخ الفاتورة الأصلية."})
        if not invoice.delivery.warehouse.is_active:
            raise serializers.ValidationError({"invoice": "مخزن الفاتورة الأصلية موقوف."})
        lines = attrs.get("lines", [])
        if not lines:
            raise serializers.ValidationError({"lines": "أضف صنفًا واحدًا على الأقل."})
        ids = [row["invoice_line"].pk for row in lines]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError({"lines": "لا تكرر بند الفاتورة في نفس المردود."})
        for row in lines:
            if row["invoice_line"].invoice_id != invoice.pk:
                raise serializers.ValidationError({"lines": "كل البنود يجب أن تكون من الفاتورة المحددة."})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        lines_data = validated_data.pop("lines")
        invoice = validated_data["invoice"]
        company = validated_data["company"]
        warehouse = invoice.delivery.warehouse
        return_number = validated_data["return_number"]
        movement = StockMovement.objects.create(
            company=company, movement_number=f"SR-{return_number}",
            movement_type=StockMovement.MovementType.RECEIPT,
            movement_date=validated_data["return_date"],
            destination_warehouse=warehouse,
            notes=validated_data.get("notes", ""), created_by=self.context["request"].user,
        )
        sales_return = SalesReturn.objects.create(
            **validated_data, warehouse=warehouse, stock_movement=movement,
            created_by=self.context["request"].user,
        )
        for row in lines_data:
            source = row["invoice_line"].delivery_line
            quantity = row["quantity"]
            unit_cost = (source.cost_amount / source.quantity).quantize(Decimal("0.000001"))
            movement_line = StockMovementLine.objects.create(
                movement=movement, product=source.product, quantity=quantity,
                unit_cost=unit_cost, destination_location=source.source_location,
                expiry_date=source.expiry_date,
            )
            original_qty = source.quantity
            invoice_line = row["invoice_line"]
            net = (invoice_line.net_amount * quantity / original_qty).quantize(Decimal("0.0001"))
            tax = (invoice_line.tax_amount * quantity / original_qty).quantize(Decimal("0.0001"))
            cost = (source.cost_amount * quantity / original_qty).quantize(Decimal("0.0001"))
            SalesReturnLine.objects.create(
                sales_return=sales_return, invoice_line=invoice_line,
                movement_line=movement_line, quantity=quantity,
                net_amount=net, tax_amount=tax, total_amount=net + tax,
                cost_amount=cost,
            )
        return sales_return

    @transaction.atomic
    def update(self, instance, validated_data):
        sales_return = SalesReturn.objects.select_for_update().get(pk=instance.pk)
        if sales_return.status != SalesReturn.Status.DRAFT:
            raise serializers.ValidationError("لا يمكن تعديل مردود مبيعات مرحّل.")
        lines_data = validated_data.pop("lines", None)
        movement = StockMovement.objects.select_for_update().get(pk=sales_return.stock_movement_id)
        if lines_data is not None:
            sales_return.lines.all().delete()
            movement.lines.all().delete()
            for row in lines_data:
                source = row["invoice_line"].delivery_line
                quantity = row["quantity"]
                unit_cost = (source.cost_amount / source.quantity).quantize(Decimal("0.000001"))
                movement_line = StockMovementLine.objects.create(
                    movement=movement, product=source.product, quantity=quantity,
                    unit_cost=unit_cost, destination_location=source.source_location,
                    expiry_date=source.expiry_date,
                )
                invoice_line = row["invoice_line"]
                net = (invoice_line.net_amount * quantity / source.quantity).quantize(Decimal("0.0001"))
                tax = (invoice_line.tax_amount * quantity / source.quantity).quantize(Decimal("0.0001"))
                cost = (source.cost_amount * quantity / source.quantity).quantize(Decimal("0.0001"))
                SalesReturnLine.objects.create(
                    sales_return=sales_return, invoice_line=invoice_line,
                    movement_line=movement_line, quantity=quantity,
                    net_amount=net, tax_amount=tax, total_amount=net + tax,
                    cost_amount=cost,
                )
        for field, value in validated_data.items():
            setattr(sales_return, field, value)
        sales_return.save()
        movement.movement_date = sales_return.return_date
        movement.notes = sales_return.notes
        movement.save(update_fields=("movement_date", "notes"))
        return sales_return
