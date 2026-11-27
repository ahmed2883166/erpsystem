from django.contrib import admin

from .models import PurchaseInvoice, PurchaseInvoiceLine, PurchaseOrder, PurchaseOrderLine, PurchaseReceipt, SupplierPayment


class PurchaseInvoiceLineInline(admin.TabularInline):
    model = PurchaseInvoiceLine
    extra = 0
    readonly_fields = ("net_amount", "tax_amount", "total_amount")


@admin.register(PurchaseReceipt)
class PurchaseReceiptAdmin(admin.ModelAdmin):
    list_display = ("receipt_number", "company", "supplier", "warehouse", "receipt_date", "stock_movement")
    list_filter = ("company", "receipt_date")
    search_fields = ("receipt_number", "supplier__name_ar", "supplier_delivery_ref")


@admin.register(PurchaseInvoice)
class PurchaseInvoiceAdmin(admin.ModelAdmin):
    list_display = ("invoice_number", "company", "supplier", "invoice_date", "status", "grand_total")
    list_filter = ("company", "status", "invoice_date")
    search_fields = ("invoice_number", "supplier__name_ar")
    inlines = (PurchaseInvoiceLineInline,)
    readonly_fields = ("status", "net_total", "tax_total", "grand_total", "journal_entry", "posted_at")


@admin.register(SupplierPayment)
class SupplierPaymentAdmin(admin.ModelAdmin):
    list_display = ("payment_number", "company", "supplier", "invoice", "payment_date", "method", "amount", "status")
    list_filter = ("company", "status", "method", "payment_date")
    search_fields = ("payment_number", "supplier__name_ar", "invoice__invoice_number", "reference")
    readonly_fields = ("status", "journal_entry", "posted_at")


class PurchaseOrderLineInline(admin.TabularInline):
    model = PurchaseOrderLine
    extra = 0
    readonly_fields = ("received_quantity",)


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    list_display = ("order_number", "company", "supplier", "warehouse", "order_date", "expected_date", "status")
    list_filter = ("company", "status", "order_date")
    search_fields = ("order_number", "supplier__name_ar")
    inlines = (PurchaseOrderLineInline,)
    readonly_fields = ("status", "confirmed_at")


from .models import LandedCostAllocation, LandedCostDocument, LandedCostDistribution


class LandedCostAllocationInline(admin.TabularInline):
    model = LandedCostAllocation
    extra = 0
    readonly_fields = ("created_at",)


class LandedCostDistributionInline(admin.TabularInline):
    model = LandedCostDistribution
    extra = 0
    readonly_fields = ("journal_entry",)


@admin.register(LandedCostDocument)
class LandedCostDocumentAdmin(admin.ModelAdmin):
    list_display = ("document_number", "company", "source_invoice", "document_date", "status", "total_amount")
    list_filter = ("company", "status", "document_date")
    search_fields = ("document_number", "source_invoice__invoice_number", "source_invoice__supplier__name_ar")
    readonly_fields = ("status", "total_amount", "posted_by", "posted_at", "reversal_journal_entry", "reversed_at")
    inlines = (LandedCostAllocationInline,)


@admin.register(LandedCostDistribution)
class LandedCostDistributionAdmin(admin.ModelAdmin):
    list_display = ("allocation", "branch", "inventory_amount", "cogs_amount", "adjustment_loss_amount", "journal_entry")
    list_filter = ("branch",)
    readonly_fields = ("journal_entry",)
