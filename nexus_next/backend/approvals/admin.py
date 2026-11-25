"""تسجيل سياسات الاعتماد وطلبات اعتماد الفواتير والعمليات المالية في لوحة الإدارة."""

from django.contrib import admin

from .models import DiscountApprovalPolicy, SalesInvoiceApproval, FinancialApprovalRequest


@admin.register(DiscountApprovalPolicy)
class DiscountApprovalPolicyAdmin(admin.ModelAdmin):
    list_display = ("company", "max_discount_percent", "max_sales_invoice_amount", "max_purchase_invoice_amount", "max_payment_amount", "updated_at")
    readonly_fields = ("company", "max_discount_percent", "max_sales_invoice_amount", "max_purchase_invoice_amount", "max_payment_amount", "created_by", "updated_by", "created_at", "updated_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SalesInvoiceApproval)
class SalesInvoiceApprovalAdmin(admin.ModelAdmin):
    list_display = ("invoice", "company", "requested_by", "status", "decided_by", "requested_at")
    readonly_fields = ("company", "invoice", "requested_by", "requested_limit", "status", "decided_by", "decision_note", "requested_at", "decided_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(FinancialApprovalRequest)
class FinancialApprovalRequestAdmin(admin.ModelAdmin):
    list_display = ("document_type", "company", "requested_amount", "requested_limit", "status", "requested_by", "decided_by", "requested_at")
    readonly_fields = (
        "company", "document_type", "purchase_invoice", "supplier_payment", "treasury_transaction",
        "requested_by", "requested_limit", "requested_amount", "requested_snapshot",
        "status", "decided_by", "decision_note", "requested_at", "decided_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
