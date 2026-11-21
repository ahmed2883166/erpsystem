from django.contrib import admin

from .models import PurchaseInvoice, PurchaseInvoiceLine, PurchaseReceipt, SupplierPayment


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
