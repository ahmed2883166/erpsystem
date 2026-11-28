"""تحقق وتحويل بيانات سياسات الاعتماد وطلبات الموافقة المتبادلة مع الواجهة."""

from decimal import Decimal

from rest_framework import serializers

from .models import DiscountApprovalPolicy, SalesInvoiceApproval


class DiscountApprovalPolicySerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source="company.name_ar", read_only=True)

    class Meta:
        model = DiscountApprovalPolicy
        fields = ("id", "company", "company_name", "max_discount_percent", "max_sales_invoice_amount", "max_purchase_invoice_amount", "max_purchase_order_amount", "max_payment_amount", "created_at", "updated_at")
        read_only_fields = ("created_at", "updated_at")

    def validate(self, attrs):
        """التحقق من صحة البيانات والعلاقات المرتبطة قبل حفظ السجل."""
        discount_limit = attrs.get(
            "max_discount_percent",
            self.instance.max_discount_percent if self.instance else None,
        )
        sales_amount_limit = attrs.get(
            "max_sales_invoice_amount",
            self.instance.max_sales_invoice_amount if self.instance else None,
        )
        purchase_amount_limit = attrs.get(
            "max_purchase_invoice_amount",
            self.instance.max_purchase_invoice_amount if self.instance else None,
        )
        purchase_order_amount_limit = attrs.get(
            "max_purchase_order_amount",
            self.instance.max_purchase_order_amount if self.instance else None,
        )
        payment_amount_limit = attrs.get(
            "max_payment_amount",
            self.instance.max_payment_amount if self.instance else None,
        )
        if all(value is None for value in (discount_limit, sales_amount_limit, purchase_amount_limit, purchase_order_amount_limit, payment_amount_limit)):
            raise serializers.ValidationError(
                "يجب تحديد حد مالي واحد على الأقل."
            )
        return attrs

    def validate_company(self, company):
        """التحقق من صحة البيانات والعلاقات المرتبطة قبل حفظ السجل."""
        if self.instance and company.pk != self.instance.company_id:
            raise serializers.ValidationError("لا يمكن نقل سياسة الخصم بين الشركات.")
        return company


class SalesInvoiceApprovalSerializer(serializers.ModelSerializer):
    invoice_number = serializers.CharField(source="invoice.invoice_number", read_only=True)
    customer_name = serializers.CharField(source="invoice.customer.name_ar", read_only=True)
    invoice_date = serializers.DateField(source="invoice.invoice_date", read_only=True)
    invoice_total = serializers.SerializerMethodField()
    requested_by_name = serializers.CharField(source="requested_by.username", read_only=True)
    decided_by_name = serializers.CharField(source="decided_by.username", read_only=True, allow_null=True)

    def get_invoice_total(self, obj):
        """تجهيز قيمة محسوبة أو بيانات مرتبطة لعرضها بأمان في الواجهة."""
        if obj.requested_lines_snapshot:
            total = sum(
                (Decimal(line.get("total_amount", "0")) for line in obj.requested_lines_snapshot),
                Decimal("0"),
            )
            return str(total)
        return str(obj.invoice.grand_total)

    class Meta:
        model = SalesInvoiceApproval
        fields = (
            "id", "company", "invoice", "invoice_number", "customer_name", "invoice_date",
            "invoice_total", "requested_by", "requested_by_name", "requested_limit",
            "requested_discount_total", "requested_max_discount_percent", "requested_invoice_limit",
            "requested_credit_limit", "requested_credit_exposure", "requested_credit_amount", "requested_lines_snapshot",
            "status", "decided_by", "decided_by_name", "decision_note",
            "requested_at", "decided_at",
        )
        read_only_fields = fields


class FinancialApprovalRequestSerializer(serializers.ModelSerializer):
    requested_by_name = serializers.CharField(source="requested_by.username", read_only=True)
    decided_by_name = serializers.CharField(source="decided_by.username", read_only=True, allow_null=True)
    document_number = serializers.SerializerMethodField()
    document_total = serializers.SerializerMethodField()

    class Meta:
        from .models import FinancialApprovalRequest
        model = FinancialApprovalRequest
        fields = (
            "id", "company", "document_type", "purchase_invoice", "purchase_order", "supplier_payment",
            "treasury_transaction", "asset_purchase_invoice", "asset_supplier_payment", "document_number", "document_total",
            "requested_by", "requested_by_name", "requested_limit", "requested_amount",
            "requested_snapshot", "status", "decided_by", "decided_by_name",
            "decision_note", "requested_at", "decided_at",
        )
        read_only_fields = fields

    def get_document_number(self, obj):
        """تجهيز قيمة محسوبة أو بيانات مرتبطة لعرضها بأمان في الواجهة."""
        document = obj.purchase_invoice or obj.purchase_order or obj.supplier_payment or obj.treasury_transaction or obj.asset_purchase_invoice or obj.asset_supplier_payment
        if obj.document_type == "purchase_invoice":
            return document.invoice_number
        if obj.document_type == "purchase_order":
            return document.order_number
        if obj.document_type == "supplier_payment":
            return document.payment_number
        if obj.document_type == "asset_purchase_invoice":
            return document.invoice_number
        if obj.document_type == "asset_supplier_payment":
            return document.payment_number
        return document.document_number

    def get_document_total(self, obj):
        """تجهيز قيمة محسوبة أو بيانات مرتبطة لعرضها بأمان في الواجهة."""
        return str(obj.requested_amount)
