from django.contrib import admin

from .models import DiscountApprovalPolicy, SalesInvoiceApproval


@admin.register(DiscountApprovalPolicy)
class DiscountApprovalPolicyAdmin(admin.ModelAdmin):
    list_display = ("company", "max_discount_percent", "updated_at")
    readonly_fields = ("created_at", "updated_at")


@admin.register(SalesInvoiceApproval)
class SalesInvoiceApprovalAdmin(admin.ModelAdmin):
    list_display = ("invoice", "company", "requested_by", "status", "decided_by", "requested_at")
    readonly_fields = ("company", "invoice", "requested_by", "requested_limit", "status", "decided_by", "decision_note", "requested_at", "decided_at")
