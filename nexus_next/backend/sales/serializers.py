from decimal import Decimal

from django.db import transaction
from django.db.models import Q, Sum
from rest_framework import serializers

from accounting.models import ChartOfAccount, TaxCode
from companies.access import resolve_warehouse_branch
from companies.models import ExchangeRate
from treasury.models import TreasuryAccount

from inventory.models import Product, StockMovement, StockMovementLine, StorageLocation
from customers.models import CustomerDiscountRule
from .models import CustomerReceipt, CustomerReceiptBatch, SalesDelivery, SalesInvoice, SalesInvoiceLine, SalesQuotation, SalesQuotationLine, SalesDocumentTemplate, SalesReturn, SalesOrder, SalesOrderLine, SalesDeliveryAllocation
from approvals.snapshots import invoice_discount_snapshot


def _customer_discount_for_line(*, customer, product, quantity, gross_invoice_amount, rules):
    """اختر قاعدة واحدة فقط؛ الأولوية ثم التخصيص الأعلى، دون تراكم خصومات."""
    matches = []
    for rule in rules:
        if rule.customer_id and rule.customer_id != customer.pk:
            continue
        if rule.customer_type and rule.customer_type != customer.customer_type:
            continue
        if rule.product_id and rule.product_id != product.pk:
            continue
        if rule.category_id and rule.category_id != product.category_id:
            continue
        if rule.min_quantity is not None and quantity < rule.min_quantity:
            continue
        if rule.min_invoice_amount is not None and gross_invoice_amount < rule.min_invoice_amount:
            continue
        specificity = sum((bool(rule.customer_id), bool(rule.customer_type), bool(rule.product_id), bool(rule.category_id), rule.min_quantity is not None, rule.min_invoice_amount is not None))
        matches.append((rule.priority, specificity, -rule.pk, rule))
    return max(matches, default=(0, 0, 0, None))[3]

class SalesOrderLineInputSerializer(serializers.Serializer):
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())
    quantity = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0.0001"))
    unit_price = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0"))
    tax_rate = serializers.DecimalField(max_digits=7, decimal_places=4, min_value=Decimal("0"), max_value=Decimal("100"), required=False, default=Decimal("0"))
    tax_code = serializers.PrimaryKeyRelatedField(queryset=TaxCode.objects.all(), required=False, allow_null=True)
    discount_percent = serializers.DecimalField(max_digits=7, decimal_places=4, min_value=Decimal("0"), max_value=Decimal("100"), required=False, default=Decimal("0"))


class SalesOrderLineSerializer(serializers.ModelSerializer):
    product_code = serializers.CharField(source="product.code", read_only=True)
    product_name = serializers.CharField(source="product.name_ar", read_only=True)
    allocated_quantity = serializers.SerializerMethodField()
    remaining_quantity = serializers.SerializerMethodField()

    def get_allocated_quantity(self, obj):
        return str(obj.delivery_allocations.aggregate(total=Sum("quantity"))["total"] or Decimal("0"))

    def get_remaining_quantity(self, obj):
        allocated = obj.delivery_allocations.aggregate(total=Sum("quantity"))["total"] or Decimal("0")
        return str(max(obj.quantity - allocated, Decimal("0")))

    class Meta:
        model = SalesOrderLine
        fields = ("id", "product", "product_code", "product_name", "quantity", "allocated_quantity",
                  "remaining_quantity", "unit_price", "tax_rate", "tax_code", "discount_percent",
                  "discount_amount", "net_amount", "tax_amount", "total_amount")
        read_only_fields = fields


class SalesOrderSerializer(serializers.ModelSerializer):
    lines = SalesOrderLineInputSerializer(many=True, write_only=True, required=False)
    order_lines = SalesOrderLineSerializer(source="lines", many=True, read_only=True)
    customer_name = serializers.CharField(source="customer.name_ar", read_only=True)
    warehouse_name = serializers.CharField(source="warehouse.name_ar", read_only=True)

    class Meta:
        model = SalesOrder
        fields = ("id", "company", "customer", "customer_name", "warehouse", "warehouse_name",
                  "order_number", "order_date", "requested_delivery_date", "status",
                  "net_total", "discount_total", "tax_total", "grand_total", "notes",
                  "lines", "order_lines", "created_at", "updated_at")
        read_only_fields = ("status", "net_total", "discount_total", "tax_total", "grand_total",
                            "order_lines", "created_at", "updated_at")

    def validate(self, attrs):
        instance = self.instance
        company = attrs.get("company", instance.company if instance else None)
        customer = attrs.get("customer", instance.customer if instance else None)
        warehouse = attrs.get("warehouse", instance.warehouse if instance else None)
        order_date = attrs.get("order_date", instance.order_date if instance else None)
        requested_date = attrs.get("requested_delivery_date", instance.requested_delivery_date if instance else None)
        if instance and instance.status != SalesOrder.Status.DRAFT:
            raise serializers.ValidationError("لا يمكن تعديل أمر بيع معتمد.")
        if instance and company.pk != instance.company_id:
            raise serializers.ValidationError({"company": "لا يمكن نقل أمر البيع إلى شركة أخرى."})
        if not customer.is_active or customer.company_id != company.pk:
            raise serializers.ValidationError({"customer": "اختر عميلًا نشطًا تابعًا للشركة."})
        if not warehouse.is_active or warehouse.company_id != company.pk:
            raise serializers.ValidationError({"warehouse": "اختر مخزنًا نشطًا تابعًا للشركة."})
        if requested_date and order_date and requested_date < order_date:
            raise serializers.ValidationError({"requested_delivery_date": "تاريخ التسليم المطلوب لا يسبق تاريخ أمر البيع."})
        lines = attrs.get("lines")
        if lines is None and not instance:
            raise serializers.ValidationError({"lines": "أضف بندًا واحدًا على الأقل."})
        if lines is not None:
            if not lines:
                raise serializers.ValidationError({"lines": "أضف بندًا واحدًا على الأقل."})
            for line in lines:
                product = line["product"]
                if product.company_id != company.pk or not product.is_active or product.product_type != Product.ProductType.GOODS:
                    raise serializers.ValidationError({"lines": "أوامر البيع تقبل أصنافًا مخزنية نشطة تتبع الشركة فقط."})
                tax_code = line.get("tax_code")
                if tax_code and (not tax_code.is_active or tax_code.company_id != company.pk or tax_code.country != company.country):
                    raise serializers.ValidationError({"lines": "كود الضريبة غير نشط أو لا يتبع الشركة."})
        return attrs

    def _save_lines(self, order, rows):
        saved = []
        for row in rows:
            line = SalesOrderLine(order=order, **row)
            line.calculate()
            line.save()
            saved.append(line)
        order.net_total = sum((line.net_amount for line in saved), Decimal("0"))
        order.discount_total = sum((line.discount_amount for line in saved), Decimal("0"))
        order.tax_total = sum((line.tax_amount for line in saved), Decimal("0"))
        order.grand_total = order.net_total + order.tax_total
        order.save(update_fields=("net_total", "discount_total", "tax_total", "grand_total", "updated_at"))

    @transaction.atomic
    def create(self, validated_data):
        rows = validated_data.pop("lines")
        order = SalesOrder.objects.create(
            **validated_data, created_by=self.context["request"].user,
        )
        self._save_lines(order, rows)
        return order

    @transaction.atomic
    def update(self, instance, validated_data):
        rows = validated_data.pop("lines", None)
        order = super().update(instance, validated_data)
        if rows is not None:
            for line in order.lines.all():
                line.delete()
            self._save_lines(order, rows)
        return order


class DeliveryLineInputSerializer(serializers.Serializer):
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())
    quantity = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0.0001"))
    source_location = serializers.IntegerField(required=False, allow_null=True)
    expiry_date = serializers.DateField(required=False, allow_null=True)
    order_line = serializers.PrimaryKeyRelatedField(queryset=SalesOrderLine.objects.select_related("order"), required=False, allow_null=True)

    def validate(self, attrs):
        """التحقق من صحة المدخلات واتساق العلاقات قبل قبول بيانات المستند."""
        product = attrs["product"]
        if product.product_type != Product.ProductType.GOODS or not product.is_active:
            raise serializers.ValidationError("اختر صنفًا مخزنيًا نشطًا.")
        order_line = attrs.get("order_line")
        if order_line and order_line.product_id != product.pk:
            raise serializers.ValidationError({"order_line": "بند أمر البيع لا يطابق الصنف المحدد."})
        if product.track_expiry and not attrs.get("expiry_date"):
            raise serializers.ValidationError({"expiry_date": "تاريخ الصلاحية مطلوب لهذا الصنف."})
        if not product.track_expiry and attrs.get("expiry_date"):
            raise serializers.ValidationError({"expiry_date": "الصنف لا يتطلب تتبع الصلاحية."})
        return attrs


class SalesDeliverySerializer(serializers.ModelSerializer):
    delivery_number = serializers.CharField(max_length=37)
    lines = DeliveryLineInputSerializer(many=True, write_only=True)
    order_number = serializers.CharField(source="order.order_number", read_only=True, allow_null=True)
    movement_status = serializers.CharField(source="stock_movement.status", read_only=True)
    customer_name = serializers.CharField(source="customer.name_ar", read_only=True)
    warehouse_name = serializers.CharField(source="warehouse.name_ar", read_only=True)
    line_items = serializers.SerializerMethodField()

    class Meta:
        model = SalesDelivery
        fields = ("id", "company", "customer", "customer_name", "warehouse", "warehouse_name",
                  "delivery_number", "delivery_date", "customer_reference", "notes", "order", "order_number",
                  "movement_status", "line_items", "quotation_snapshot", "order_snapshot", "lines", "created_at")
        read_only_fields = ("movement_status", "line_items", "quotation_snapshot", "order_snapshot", "created_at")

    def get_line_items(self, obj):
        """تجهيز تفاصيل الأصناف المرتبطة بالمستند بصيغة مناسبة للواجهة."""
        return [{
            "id": line.pk, "product": line.product_id, "product_code": line.product.code,
            "product_name": line.product.name_ar, "quantity": str(line.quantity),
            "expiry_date": line.expiry_date.isoformat() if line.expiry_date else None,
            "order_line": getattr(getattr(line, "sales_order_allocation", None), "order_line_id", None),
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
        if instance and company.pk != instance.company_id:
            raise serializers.ValidationError({"company": "لا يمكن نقل عرض السعر إلى شركة أخرى."})
        if customer and (customer.company_id != company.pk or not customer.is_active):
            raise serializers.ValidationError({"customer": "اختر عميلًا نشطًا تابعًا للشركة."})
        if warehouse and (warehouse.company_id != company.pk or not warehouse.is_active):
            raise serializers.ValidationError({"warehouse": "اختر مخزنًا نشطًا تابعًا للشركة."})
        order = attrs.get("order", instance.order if instance else None)
        if instance and "order" in attrs and attrs["order"] != instance.order:
            raise serializers.ValidationError({"order": "لا يمكن تغيير أمر البيع المرتبط بإذن التسليم."})
        if order:
            if order.company_id != company.pk or order.customer_id != customer.pk or order.warehouse_id != warehouse.pk:
                raise serializers.ValidationError({"order": "أمر البيع يجب أن يطابق الشركة والعميل والمخزن."})
            if order.status not in (SalesOrder.Status.CONFIRMED, SalesOrder.Status.PARTIALLY_DELIVERED):
                raise serializers.ValidationError({"order": "لا يمكن التسليم إلا من أمر بيع معتمد وغير مكتمل."})
            delivery_date = attrs.get("delivery_date", instance.delivery_date if instance else None)
            if delivery_date and delivery_date < order.order_date:
                raise serializers.ValidationError({"delivery_date": "تاريخ التسليم لا يسبق تاريخ أمر البيع."})
        lines = attrs.get("lines")
        if lines is None:
            if instance:
                return attrs
            raise serializers.ValidationError({"lines": "أضف صنفًا واحدًا على الأقل."})
        if not lines:
            raise serializers.ValidationError({"lines": "أضف صنفًا واحدًا على الأقل."})
        if order:
            if instance and "lines" in attrs:
                raise serializers.ValidationError({"lines": "لا يمكن تغيير بنود إذن مرتبط بأمر بيع؛ أنشئ إذنًا جديدًا للكميات المتبقية."})
            if not instance:
                self._validate_order_allocations(order, lines)
        elif any(line.get("order_line") for line in lines):
            raise serializers.ValidationError({"lines": "حدد أمر البيع عند ربط بنوده بإذن التسليم."})
        for line in lines:
            if line["product"].company_id != company.pk or not line["product"].is_active or line["product"].product_type != Product.ProductType.GOODS:
                raise serializers.ValidationError({"lines": "اختر أصنافًا مخزنية نشطة تتبع الشركة؛ الخدمات غير مدعومة في دورة التسليم الحالية."})
            if order and (not line.get("order_line") or line["order_line"].order_id != order.pk):
                raise serializers.ValidationError({"lines": "كل صنف في إذن مرتبط بأمر بيع يجب ربطه ببند من نفس الأمر."})
            location_id = line.get("source_location")
            if location_id:
                location = StorageLocation.objects.filter(
                    pk=location_id, warehouse=warehouse, is_active=True
                ).first()
                if not location:
                    raise serializers.ValidationError({"lines": "موقع الصرف غير نشط أو لا يتبع المخزن."})
                line["source_location_obj"] = location
        return attrs

    def _validate_order_allocations(self, order, lines, exclude_delivery_id=None):
        totals = {}
        for row in lines:
            order_line = row.get("order_line")
            if not order_line or order_line.order_id != order.pk or order_line.product_id != row["product"].pk:
                raise serializers.ValidationError({"lines": "اربط كل صنف ببند صحيح من أمر البيع."})
            if order_line.pk in totals:
                raise serializers.ValidationError({"lines": "لا تكرر بند أمر البيع داخل إذن واحد؛ اجمع الكمية في سطر واحد."})
            totals[order_line.pk] = row["quantity"]
        locked_lines = {
            line.pk: line for line in SalesOrderLine.objects.filter(
                pk__in=sorted(totals), order_id=order.pk
            )
        }
        for line_id, requested in totals.items():
            line = locked_lines.get(line_id)
            if not line:
                raise serializers.ValidationError({"lines": "بند أمر البيع غير موجود."})
            delivered = SalesDeliveryAllocation.objects.filter(order_line_id=line_id)
            if exclude_delivery_id:
                delivered = delivered.exclude(delivery_id=exclude_delivery_id)
            already_allocated = delivered.aggregate(total=Sum("quantity"))["total"] or Decimal("0")
            if already_allocated + requested > line.quantity:
                remaining = line.quantity - already_allocated
                raise serializers.ValidationError({
                    "lines": f"الكمية المطلوبة تتجاوز المتبقي في أمر البيع للصنف {line.product.name_ar}. المتبقي: {remaining}."
                })

    @transaction.atomic
    def create(self, validated_data):
        """إنشاء المستند وبنوده داخل معاملة ذرية لضمان اتساق البيانات."""
        lines = validated_data.pop("lines")
        company, warehouse = validated_data["company"], validated_data["warehouse"]
        order = validated_data.get("order")
        if order:
            order = SalesOrder.objects.select_for_update().get(pk=order.pk)
            if order.status not in (SalesOrder.Status.CONFIRMED, SalesOrder.Status.PARTIALLY_DELIVERED):
                raise serializers.ValidationError({"order": "أمر البيع غير متاح للتسليم."})
            self._validate_order_allocations(order, lines)
            validated_data["order"] = order
        user = self.context["request"].user
        movement = StockMovement.objects.create(
            company=company, movement_number=f"SD-{validated_data['delivery_number']}",
            movement_type=StockMovement.MovementType.ISSUE, movement_date=validated_data["delivery_date"],
            source_warehouse=warehouse, notes=validated_data.get("notes", ""), created_by=user,
        )
        order_snapshot = {}
        if order:
            order_snapshot = {
                "order_id": order.pk,
                "order_number": order.order_number,
                "order_date": order.order_date.isoformat(),
                "lines": [{
                    "order_line_id": row["order_line"].pk,
                    "product": row["product"].pk,
                    "quantity": str(row["quantity"]),
                    "unit_price": str(row["order_line"].unit_price),
                    "tax_rate": str(row["order_line"].tax_rate),
                    "tax_code": row["order_line"].tax_code_id,
                    "discount_percent": str(row["order_line"].discount_percent),
                    "discount_rule": None,
                } for row in lines],
            }
        delivery = SalesDelivery.objects.create(
            **validated_data, stock_movement=movement, created_by=user,
            order_snapshot=order_snapshot,
        )
        if order:
            for row in lines:
                movement_line = StockMovementLine.objects.create(
                    movement=movement, product=row["product"], quantity=row["quantity"],
                    source_location=row.get("source_location_obj"), expiry_date=row.get("expiry_date"),
                )
                SalesDeliveryAllocation.objects.create(
                    delivery=delivery, order_line=row["order_line"],
                    movement_line=movement_line, quantity=row["quantity"],
                )
        else:
            StockMovementLine.objects.bulk_create([
                StockMovementLine(
                    movement=movement, product=line["product"], quantity=line["quantity"],
                    source_location=line.get("source_location_obj"), expiry_date=line.get("expiry_date"),
                ) for line in lines
            ])
        return delivery

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



class SalesQuotationLineInputSerializer(serializers.Serializer):
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())
    quantity = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0.0001"))
    unit_price = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0"))
    tax_rate = serializers.DecimalField(max_digits=7, decimal_places=4, min_value=Decimal("0"), max_value=Decimal("100"), required=False, default=Decimal("0"))
    tax_code = serializers.PrimaryKeyRelatedField(queryset=TaxCode.objects.all(), required=False, allow_null=True)
    discount_percent = serializers.DecimalField(max_digits=7, decimal_places=4, min_value=Decimal("0"), max_value=Decimal("100"), required=False)


class SalesQuotationLineSerializer(serializers.ModelSerializer):
    product_code = serializers.CharField(source="product.code", read_only=True)
    product_name = serializers.CharField(source="product.name_ar", read_only=True)
    discount_rule_name = serializers.CharField(source="discount_rule.notes", read_only=True, allow_null=True)

    class Meta:
        model = SalesQuotationLine
        fields = ("id", "product", "product_code", "product_name", "quantity", "unit_price",
                  "tax_rate", "tax_code", "discount_percent", "discount_rule",
                  "discount_rule_name", "discount_amount", "net_amount", "tax_amount", "total_amount")


class SalesQuotationSerializer(serializers.ModelSerializer):
    lines = SalesQuotationLineInputSerializer(many=True, write_only=True, required=False)
    quotation_lines = SalesQuotationLineSerializer(source="lines", many=True, read_only=True)
    customer_name = serializers.CharField(source="customer.name_ar", read_only=True)
    converted_delivery_number = serializers.CharField(source="converted_delivery.delivery_number", read_only=True, allow_null=True)

    class Meta:
        model = SalesQuotation
        fields = ("id", "company", "customer", "customer_name", "quotation_number",
                  "quotation_date", "valid_until", "status", "net_total", "discount_total",
                  "tax_total", "grand_total", "notes", "quotation_lines", "lines",
                  "converted_delivery", "converted_delivery_number", "print_template_snapshot", "created_at",
                  "updated_at", "sent_at", "accepted_at", "converted_at")
        read_only_fields = ("status", "net_total", "discount_total", "tax_total", "grand_total",
                            "quotation_lines", "converted_delivery", "converted_delivery_number",
                            "created_at", "updated_at", "sent_at", "accepted_at", "converted_at", "print_template_snapshot")

    def validate(self, attrs):
        attrs = super().validate(attrs)
        instance = self.instance
        company = attrs.get("company", instance.company if instance else None)
        customer = attrs.get("customer", instance.customer if instance else None)
        if instance and company.pk != instance.company_id:
            raise serializers.ValidationError({"company": "لا يمكن نقل عرض السعر إلى شركة أخرى."})
        quote_date = attrs.get("quotation_date", instance.quotation_date if instance else None)
        valid_until = attrs.get("valid_until", instance.valid_until if instance else None)
        if instance and instance.status != SalesQuotation.Status.DRAFT:
            raise serializers.ValidationError("لا يمكن تعديل عرض سعر غير مسودة.")
        if instance and (
            customer.pk != instance.customer_id
            or quote_date != instance.quotation_date
        ) and "lines" not in attrs:
            raise serializers.ValidationError({
                "lines": "عند تغيير العميل أو تاريخ العرض، أعد إرسال البنود لإعادة احتساب الخصومات والضرائب."
            })
        if customer and (customer.company_id != company.pk or not customer.is_active):
            raise serializers.ValidationError({"customer": "اختر عميلًا نشطًا تابعًا للشركة."})
        if quote_date and valid_until and valid_until < quote_date:
            raise serializers.ValidationError({"valid_until": "تاريخ الصلاحية لا يسبق تاريخ العرض."})
        lines = attrs.get("lines")
        if lines is not None:
            if not lines:
                raise serializers.ValidationError({"lines": "أضف بندًا واحدًا على الأقل."})
            for line in lines:
                if line["product"].company_id != company.pk:
                    raise serializers.ValidationError({"lines": "كل الأصناف يجب أن تتبع الشركة."})
                tax_code = line.get("tax_code")
                if tax_code and (tax_code.company_id != company.pk or tax_code.country != company.country):
                    raise serializers.ValidationError({"lines": "كود الضريبة لا يتبع الشركة المحددة."})
        elif not instance:
            raise serializers.ValidationError({"lines": "أضف بندًا واحدًا على الأقل."})
        return attrs

    def _save_lines(self, quotation, lines):
        gross_total = sum((row["quantity"] * row["unit_price"] for row in lines), Decimal("0"))
        rules = list(
            CustomerDiscountRule.objects.filter(company=quotation.company, is_active=True,
                effective_from__lte=quotation.quotation_date)
            .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=quotation.quotation_date))
            .filter(Q(customer__isnull=True) | Q(customer=quotation.customer))
            .filter(Q(customer_type="") | Q(customer_type=quotation.customer.customer_type))
            .filter(Q(min_invoice_amount__isnull=True) | Q(min_invoice_amount__lte=gross_total))
            .order_by("-priority", "id")
        )
        for row in lines:
            rule = None
            if "discount_percent" in row:
                discount = row["discount_percent"]
            else:
                rule = _customer_discount_for_line(
                    customer=quotation.customer, product=row["product"], quantity=row["quantity"],
                    gross_invoice_amount=gross_total, rules=rules,
                )
                discount = rule.discount_percent if rule else quotation.customer.default_discount_percent
            line = SalesQuotationLine.objects.create(
                quotation=quotation, product=row["product"], quantity=row["quantity"],
                unit_price=row["unit_price"], tax_rate=row.get("tax_rate", Decimal("0")),
                tax_code=row.get("tax_code"), discount_percent=discount, discount_rule=rule,
            )
            line.calculate()
            line.save(update_fields=("tax_rate", "discount_amount", "net_amount", "tax_amount", "total_amount"))
        invoice_lines = list(quotation.lines.all())
        quotation.net_total = sum((line.net_amount for line in invoice_lines), Decimal("0"))
        quotation.discount_total = sum((line.discount_amount for line in invoice_lines), Decimal("0"))
        quotation.tax_total = sum((line.tax_amount for line in invoice_lines), Decimal("0"))
        quotation.grand_total = quotation.net_total + quotation.tax_total
        quotation.save(update_fields=("net_total", "discount_total", "tax_total", "grand_total", "updated_at"))

    @transaction.atomic
    def create(self, validated_data):
        lines = validated_data.pop("lines")
        quotation = SalesQuotation.objects.create(**validated_data, created_by=self.context["request"].user)
        self._save_lines(quotation, lines)
        return quotation

    @transaction.atomic
    def update(self, instance, validated_data):
        quotation = SalesQuotation.objects.select_for_update().get(pk=instance.pk)
        if quotation.status != SalesQuotation.Status.DRAFT:
            raise serializers.ValidationError("لا يمكن تعديل عرض سعر غير مسودة.")
        lines = validated_data.pop("lines", None)
        for field, value in validated_data.items():
            setattr(quotation, field, value)
        quotation.save()
        if lines is not None:
            for old_line in quotation.lines.all():
                old_line.delete()
            self._save_lines(quotation, lines)
        return quotation



class SalesDocumentTemplateSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source="company.name_ar", read_only=True)

    class Meta:
        model = SalesDocumentTemplate
        fields = ("id", "company", "company_name", "document_type", "name", "style",
                  "config", "is_default", "is_active", "created_at", "updated_at")
        read_only_fields = ("created_at", "updated_at")
        validators = []

    def validate_config(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("إعدادات القالب يجب أن تكون كائنًا.")
        allowed = {"accent_color", "show_logo", "show_tax_number", "show_discount",
                   "show_tax", "show_bank_details", "bank_details", "footer_text", "language", "font_scale"}
        unknown = set(value) - allowed
        if unknown:
            raise serializers.ValidationError("توجد إعدادات غير مدعومة: " + ", ".join(sorted(unknown)))
        color = value.get("accent_color")
        if color is not None and (
            not isinstance(color, str) or len(color) not in (4, 7)
            or not color.startswith("#")
            or any(ch not in "0123456789abcdefABCDEF" for ch in color[1:])
        ):
            raise serializers.ValidationError({"accent_color": "أدخل لونًا بصيغة HEX مثل #1F4E79."})
        if value.get("language", "ar") not in ("ar", "en", "bilingual"):
            raise serializers.ValidationError({"language": "لغة الطباعة غير مدعومة."})
        if value.get("font_scale", "normal") not in ("small", "normal", "large"):
            raise serializers.ValidationError({"font_scale": "حجم الخط غير مدعوم."})
        if "bank_details" in value and (not isinstance(value["bank_details"], str) or len(value["bank_details"]) > 500):
            raise serializers.ValidationError({"bank_details": "بيانات البنك يجب ألا تتجاوز 500 حرف."})
        if "footer_text" in value and (not isinstance(value["footer_text"], str) or len(value["footer_text"]) > 500):
            raise serializers.ValidationError({"footer_text": "نص التذييل يجب ألا يتجاوز 500 حرف."})
        for flag in ("show_logo", "show_tax_number", "show_discount", "show_tax", "show_bank_details"):
            if flag in value and not isinstance(value[flag], bool):
                raise serializers.ValidationError({flag: "القيمة يجب أن تكون نعم أو لا."})
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        company = attrs.get("company", self.instance.company if self.instance else None)
        if self.instance and company.pk != self.instance.company_id:
            raise serializers.ValidationError({"company": "لا يمكن نقل القالب إلى شركة أخرى."})
        document_type = attrs.get("document_type", self.instance.document_type if self.instance else None)
        name = attrs.get("name", self.instance.name if self.instance else None)
        duplicate = SalesDocumentTemplate.objects.filter(company=company, document_type=document_type, name=name)
        if self.instance:
            duplicate = duplicate.exclude(pk=self.instance.pk)
        if duplicate.exists():
            raise serializers.ValidationError({"name": "يوجد قالب بنفس الاسم لهذا النوع من المستندات."})
        is_default = attrs.get("is_default", self.instance.is_default if self.instance else False)
        is_active = attrs.get("is_active", self.instance.is_active if self.instance else True)
        if is_default and not is_active:
            raise serializers.ValidationError({"is_default": "القالب الافتراضي يجب أن يكون نشطًا."})
        return attrs


class SalesInvoiceLineInputSerializer(serializers.Serializer):
    delivery_line = serializers.PrimaryKeyRelatedField(queryset=StockMovementLine.objects.select_related("movement", "product"), required=False)
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all(), required=False)
    quantity = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0.0001"), required=False)
    unit_price = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0"), required=False)
    transaction_unit_price = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0"), required=False)
    tax_rate = serializers.DecimalField(max_digits=7, decimal_places=4, min_value=Decimal("0"),
                                        max_value=Decimal("100"), required=False)
    tax_code = serializers.PrimaryKeyRelatedField(queryset=TaxCode.objects.all(), required=False, allow_null=True)
    discount_percent = serializers.DecimalField(max_digits=7, decimal_places=4, min_value=Decimal("0"), max_value=Decimal("100"), required=False)


class SalesInvoiceLineSerializer(serializers.ModelSerializer):
    product_code = serializers.CharField(source="product.code", read_only=True)
    product_name = serializers.CharField(source="product.name_ar", read_only=True)
    discount_rule_name = serializers.CharField(source="discount_rule.notes", read_only=True, allow_null=True)
    transaction_discount_amount = serializers.SerializerMethodField()

    class Meta:
        model = SalesInvoiceLine
        fields = ("id", "delivery_line", "product", "product_code", "product_name", "quantity",
                  "unit_price", "transaction_unit_price", "transaction_net_amount", "transaction_discount_amount", "transaction_tax_amount", "transaction_total_amount", "tax_rate", "tax_code", "discount_percent", "discount_rule", "discount_rule_name", "discount_amount", "net_amount", "tax_amount", "total_amount")

    def get_transaction_discount_amount(self, obj):
        if obj.transaction_unit_price is None or obj.transaction_net_amount is None:
            return None
        gross = obj.quantity * obj.transaction_unit_price
        return str((gross - obj.transaction_net_amount).quantize(Decimal("0.0001")))


class SalesInvoiceSerializer(serializers.ModelSerializer):
    lines = SalesInvoiceLineInputSerializer(many=True, write_only=True)
    invoice_lines = SalesInvoiceLineSerializer(source="lines", many=True, read_only=True)
    customer_name = serializers.CharField(source="customer.name_ar", read_only=True)
    delivery_number = serializers.CharField(source="delivery.delivery_number", read_only=True, allow_null=True)
    journal_number = serializers.CharField(source="journal_entry.entry_number", read_only=True, allow_null=True)
    paid_total = serializers.SerializerMethodField()
    balance_due = serializers.SerializerMethodField()
    approval_status = serializers.SerializerMethodField()
    transaction_currency_code = serializers.CharField(source="transaction_currency.code", read_only=True, allow_null=True)
    transaction_discount_total = serializers.SerializerMethodField()

    class Meta:
        model = SalesInvoice
        fields = ("id", "company", "customer", "customer_name", "delivery", "delivery_number", "branch",
                  "invoice_number", "invoice_date", "transaction_currency", "transaction_currency_code", "exchange_rate", "exchange_rate_date", "transaction_net_total", "transaction_discount_total", "transaction_tax_total", "transaction_grand_total", "status", "net_total", "discount_total", "discount_approval_limit", "invoice_approval_limit", "tax_total", "grand_total",
                  "paid_total", "balance_due", "approval_status", "notes", "journal_entry", "journal_number",
                  "invoice_lines", "lines", "print_template_snapshot", "created_at", "posted_at")
        read_only_fields = ("status", "transaction_net_total", "transaction_tax_total", "transaction_grand_total", "net_total", "discount_total", "discount_approval_limit", "invoice_approval_limit", "tax_total", "grand_total",
                            "balance_due", "journal_entry", "journal_number", "invoice_lines",
                            "created_at", "posted_at", "print_template_snapshot")

    def get_transaction_discount_total(self, obj):
        if not obj.transaction_currency_id:
            return None
        total = sum((
            line.quantity * line.transaction_unit_price - line.transaction_net_amount
            for line in obj.lines.all()
            if line.transaction_unit_price is not None and line.transaction_net_amount is not None
        ), Decimal("0"))
        return str(total.quantize(Decimal("0.0001")))

    def _paid_amount(self, obj):
        direct_paid = (
            sum((r.carrying_base_amount or Decimal("0") for r in obj.receipts.all() if r.status == CustomerReceipt.Status.POSTED), Decimal("0"))
            if obj.transaction_currency_id else
            sum((r.amount for r in obj.receipts.all() if r.status == CustomerReceipt.Status.POSTED), Decimal("0"))
        )
        batch_paid = sum(
            (row.amount for row in obj.receipt_batch_allocations.all()
             if row.batch.status == CustomerReceiptBatch.Status.POSTED),
            Decimal("0"),
        )
        return direct_paid + batch_paid

    def get_paid_total(self, obj):
        """حساب إجمالي التحصيلات المباشرة والمخصصة من سندات القبض المجمعة."""
        return str(self._paid_amount(obj))

    def get_balance_due(self, obj):
        """حساب الرصيد المتبقي بعد خصم التحصيلات والمردودات المرحلة."""
        paid = self._paid_amount(obj)
        returned = obj.returns.filter(
            status=SalesReturn.Status.POSTED,
        ).aggregate(total=Sum("grand_total"))["total"] or Decimal("0")
        return str(max(obj.grand_total - paid - returned, Decimal("0")))

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

    def _validate_delivery_quantities(self, lines, exclude_invoice_id=None):
        """تحقق من الكميات المفوترة والمحجوزة لكل بند تسليم."""
        for line in lines:
            delivery_line = line.get("delivery_line")
            if not delivery_line:
                continue
            quantity = line.get("quantity")
            if quantity is None:
                quantity = delivery_line.quantity
            if quantity <= 0 or quantity > delivery_line.quantity:
                raise serializers.ValidationError({
                    "lines": f"كمية الفاتورة للصنف {delivery_line.product.code} يجب أن تكون أكبر من صفر ولا تتجاوز كمية التسليم."
                })
            prior = SalesInvoiceLine.objects.filter(delivery_line_id=delivery_line.pk)
            if exclude_invoice_id:
                prior = prior.exclude(invoice_id=exclude_invoice_id)
            invoiced = prior.aggregate(total=Sum("quantity"))["total"] or Decimal("0")
            if invoiced + quantity > delivery_line.quantity:
                remaining = delivery_line.quantity - invoiced
                raise serializers.ValidationError({
                    "lines": f"الكمية المتبقية للفوترة للصنف {delivery_line.product.code} هي {remaining} فقط."
                })

    def validate(self, attrs):
        """التحقق من صحة المدخلات واتساق العلاقات قبل قبول بيانات المستند."""
        instance = self.instance
        company = attrs.get("company", instance.company if instance else None)
        customer = attrs.get("customer", instance.customer if instance else None)
        delivery = attrs.get("delivery", instance.delivery if instance else None)
        branch = attrs.get("branch", instance.branch if instance else None)
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
            if "lines" not in attrs:
                return attrs

        if delivery:
            if delivery.stock_movement.status != StockMovement.Status.POSTED:
                raise serializers.ValidationError({"delivery": "يجب ترحيل إذن التسليم أولًا."})
            if invoice_date < delivery.delivery_date:
                raise serializers.ValidationError({"invoice_date": "تاريخ الفاتورة لا يجوز أن يسبق تاريخ إذن التسليم."})
            if delivery.company_id != company.pk or delivery.customer_id != customer.pk:
                raise serializers.ValidationError({"delivery": "إذن التسليم لا يتبع العميل والشركة."})
            resolved_branch = resolve_warehouse_branch(company, delivery.warehouse)
            if branch and resolved_branch and branch.pk != resolved_branch.pk:
                raise serializers.ValidationError({"branch": "فرع الفاتورة لا يطابق فرع إذن التسليم أو الفرع الافتراضي للمخزن."})
        elif not branch or branch.company_id != company.pk or not branch.is_active:
            raise serializers.ValidationError({"branch": "حدد فرعًا نشطًا تابعًا للشركة لفاتورة الخدمة."})
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
        if not lines:
            raise serializers.ValidationError({"lines": "أضف بندًا واحدًا على الأقل."})
        if delivery:
            expected = set(delivery.stock_movement.lines.values_list("id", flat=True))
            supplied = [line.get("delivery_line").pk for line in lines if line.get("delivery_line")]
            if (
                len(supplied) != len(lines) or not supplied
                or len(supplied) != len(set(supplied))
                or not set(supplied).issubset(expected)
            ):
                raise serializers.ValidationError({"lines": "اختر بنودًا صحيحة من إذن التسليم دون تكرار."})
            self._validate_delivery_quantities(
                lines, exclude_invoice_id=instance.pk if instance else None
            )
        else:
            for line in lines:
                product = line.get("product")
                if not product or product.product_type != Product.ProductType.SERVICE or product.company_id != company.pk or not product.is_active:
                    raise serializers.ValidationError({"lines": "فاتورة الخدمة تقبل خدمات تابعة للشركة فقط، دون بنود مخزنية."})
                quantity = line.get("quantity")
                if not quantity:
                    raise serializers.ValidationError({"lines": "الكمية مطلوبة لكل خدمة."})
                quantum = Decimal("1").scaleb(-product.unit.decimal_places)
                if quantity != quantity.quantize(quantum):
                    raise serializers.ValidationError({
                        "lines": f"كمية الخدمة {product.code} تتجاوز دقة وحدة القياس ({product.unit.decimal_places} منازل عشرية)."
                    })
        for line in lines:
            if transaction_currency and line.get("transaction_unit_price") is None:
                raise serializers.ValidationError({"lines": "سعر الوحدة بالعملة الأجنبية مطلوب لكل بند."})
            if not transaction_currency and line.get("unit_price") is None:
                raise serializers.ValidationError({"lines": "سعر الوحدة بالعملة الأساسية مطلوب لكل بند."})
            if delivery and line.get("product") and line["product"].pk != line["delivery_line"].product_id:
                raise serializers.ValidationError({"lines": "الصنف لا يطابق بند إذن التسليم."})
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
        delivery = validated_data.get("delivery")
        if delivery:
            line_ids = [line["delivery_line"].pk for line in lines]
            locked_lines = list(
                StockMovementLine.objects.select_for_update()
                .filter(pk__in=line_ids, movement_id=delivery.stock_movement_id)
                .order_by("pk")
            )
            if len(locked_lines) != len(set(line_ids)):
                raise serializers.ValidationError({"lines": "بعض بنود الفاتورة لا تتبع إذن التسليم المحدد."})
            self._validate_delivery_quantities(lines)
        invoice = SalesInvoice.objects.create(**validated_data, created_by=self.context["request"].user)
        policy = DiscountApprovalPolicy.objects.filter(company=invoice.company).first()
        discount_limits = [
            value for value in (
                policy.max_discount_percent if policy else None,
                invoice.customer.discount_approval_limit,
            ) if value is not None
        ]
        invoice.discount_approval_limit = min(discount_limits) if discount_limits else None
        if policy:
            invoice.invoice_approval_limit = policy.max_sales_invoice_amount
        invoice.save(update_fields=("discount_approval_limit", "invoice_approval_limit"))
        gross_invoice_amount = Decimal("0")
        for line in lines:
            base_unit_price = line.get("unit_price")
            if base_unit_price is None and line.get("transaction_unit_price") is not None and invoice.exchange_rate:
                base_unit_price = line["transaction_unit_price"] * invoice.exchange_rate
            quantity = line.get("quantity") or (line["delivery_line"].quantity if line.get("delivery_line") else Decimal("0"))
            gross_invoice_amount += quantity * (base_unit_price or Decimal("0"))
        discount_rules = list(
            CustomerDiscountRule.objects.filter(company=invoice.company, is_active=True, effective_from__lte=invoice.invoice_date)
            .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=invoice.invoice_date))
            .filter(Q(customer__isnull=True) | Q(customer=invoice.customer))
            .filter(Q(customer_type="") | Q(customer_type=invoice.customer.customer_type))
            .filter(Q(min_invoice_amount__isnull=True) | Q(min_invoice_amount__lte=gross_invoice_amount))
            .order_by("-priority", "id")
        )
        for line in lines:
            delivery_line = line.get("delivery_line")
            product = line.get("product") or delivery_line.product
            quantity = line.get("quantity") or (delivery_line.quantity if delivery_line else Decimal("1"))
            selected_rule = None
            if "discount_percent" in line:
                discount_percent = line["discount_percent"]
            else:
                selected_rule = _customer_discount_for_line(
                    customer=invoice.customer, product=product, quantity=quantity,
                    gross_invoice_amount=gross_invoice_amount, rules=discount_rules,
                )
                discount_percent = selected_rule.discount_percent if selected_rule else invoice.customer.default_discount_percent
            SalesInvoiceLine.objects.create(
                invoice=invoice,
                unit_price=line.get("unit_price", Decimal("0")),
                transaction_unit_price=line.get("transaction_unit_price"),
                delivery_line=delivery_line,
                product=product,
                quantity=quantity,
                tax_rate=line.get("tax_rate", Decimal("0")),
                tax_code=line.get("tax_code"),
                discount_percent=discount_percent,
                discount_rule=selected_rule,
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
            if instance.delivery_id:
                line_ids = [line["delivery_line"].pk for line in lines if line.get("delivery_line")]
                locked_lines = list(
                    StockMovementLine.objects.select_for_update()
                    .filter(pk__in=line_ids, movement_id=instance.delivery.stock_movement_id)
                    .order_by("pk")
                )
                if len(locked_lines) != len(set(line_ids)):
                    raise serializers.ValidationError({"lines": "بعض بنود الفاتورة لا تتبع إذن التسليم المحدد."})
                self._validate_delivery_quantities(lines, exclude_invoice_id=instance.pk)
            existing = list(instance.lines.order_by("id"))
            if len(existing) != len(lines):
                raise serializers.ValidationError({"lines": "لا يمكن إضافة أو حذف بنود من فاتورة موجودة؛ أنشئ الفاتورة من جديد قبل الترحيل."})
            for line, data in zip(existing, lines):
                delivery_line = data.pop("delivery_line", None)
                product = data.pop("product", None)
                if delivery_line is not None:
                    line.delivery_line = delivery_line
                    line.product = delivery_line.product
                    line.quantity = data.get("quantity", line.quantity)
                else:
                    if product is not None:
                        line.product = product
                    line.quantity = data.get("quantity", line.quantity)
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
    transaction_currency_code = serializers.CharField(source="transaction_currency.code", read_only=True, allow_null=True)
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
                  "receipt_number", "receipt_date", "method", "payment_account", "payment_account_name", "amount", "transaction_currency", "transaction_currency_code", "transaction_amount", "exchange_rate", "exchange_rate_date", "carrying_base_amount", "historical_carrying_base_amount", "realized_fx_gain", "realized_fx_loss", "reference", "status",
                  "journal_entry", "journal_number", "created_at", "posted_at", "reversal_journal_entry")
        read_only_fields = ("status", "carrying_base_amount", "historical_carrying_base_amount", "realized_fx_gain", "realized_fx_loss", "journal_entry", "journal_number", "created_at", "posted_at")

    def validate(self, attrs):
        """التحقق من صحة المدخلات واتساق العلاقات قبل قبول بيانات المستند."""
        company, customer, invoice = attrs.get("company"), attrs.get("customer"), attrs.get("invoice")
        if not customer.is_active:
            raise serializers.ValidationError({"customer": "العميل موقوف."})
        if invoice.status != SalesInvoice.Status.POSTED:
            raise serializers.ValidationError({"invoice": "اختر فاتورة مبيعات مرحلة."})
        receipt_date = attrs.get("receipt_date")
        if receipt_date and receipt_date < invoice.invoice_date:
            raise serializers.ValidationError({
                "receipt_date": "تاريخ سند القبض لا يجوز أن يسبق تاريخ فاتورة المبيعات."
            })
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
            except ValueError:
                raise serializers.ValidationError({"exchange_rate": "بيانات سعر الصرف غير صالحة أو لا تطابق السعر المعتمد."}) from None
            if base_amount != attrs.get("amount"):
                raise serializers.ValidationError({"amount": "المبلغ الأساسي لا يطابق المبلغ الأجنبي مضروبًا في سعر الصرف."})
        elif any(attrs.get(field) is not None for field in ("transaction_currency", "transaction_amount", "exchange_rate", "exchange_rate_date")):
            raise serializers.ValidationError({"transaction_currency": "لا تستخدم بيانات عملة أجنبية مع فاتورة بالعملة الأساسية."})
        account = attrs.get("payment_account")
        if account:
            expected_kind = TreasuryAccount.Kind.CASH if attrs.get("method") == CustomerReceipt.Method.CASH else TreasuryAccount.Kind.BANK
            treasury_account = TreasuryAccount.objects.filter(company=company, account=account, kind=expected_kind, is_active=True).first()
            if invoice.delivery_id:
                branch = resolve_warehouse_branch(company, invoice.delivery.warehouse)
            else:
                branch = invoice.branch
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
