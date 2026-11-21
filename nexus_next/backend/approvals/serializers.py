from rest_framework import serializers

from companies.models import Company
from .models import DiscountApprovalPolicy, SalesInvoiceApproval


class DiscountApprovalPolicySerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source="company.name_ar", read_only=True)

    class Meta:
        model = DiscountApprovalPolicy
        fields = ("id", "company", "company_name", "max_discount_percent", "created_at", "updated_at")
        read_only_fields = ("created_at", "updated_at")

    def validate_company(self, company):
        if self.instance and company.pk != self.instance.company_id:
            raise serializers.ValidationError("لا يمكن نقل سياسة الخصم بين الشركات.")
        return company


class SalesInvoiceApprovalSerializer(serializers.ModelSerializer):
    invoice_number = serializers.CharField(source="invoice.invoice_number", read_only=True)
    customer_name = serializers.CharField(source="invoice.customer.name_ar", read_only=True)
    invoice_date = serializers.DateField(source="invoice.invoice_date", read_only=True)
    invoice_total = serializers.DecimalField(source="invoice.grand_total", max_digits=20, decimal_places=4, read_only=True)
    requested_by_name = serializers.CharField(source="requested_by.username", read_only=True)
    decided_by_name = serializers.CharField(source="decided_by.username", read_only=True, allow_null=True)

    class Meta:
        model = SalesInvoiceApproval
        fields = (
            "id", "company", "invoice", "invoice_number", "customer_name", "invoice_date",
            "invoice_total", "requested_by", "requested_by_name", "requested_limit",
            "status", "decided_by", "decided_by_name", "decision_note",
            "requested_at", "decided_at",
        )
        read_only_fields = fields
