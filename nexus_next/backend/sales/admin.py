from django.contrib import admin

from .models import CustomerReceipt, SalesDelivery, SalesInvoice, SalesInvoiceLine


class SalesInvoiceLineInline(admin.TabularInline):
    model = SalesInvoiceLine
    extra = 0
    can_delete = False


@admin.register(SalesDelivery)
class SalesDeliveryAdmin(admin.ModelAdmin):
    list_display = ("delivery_number", "company", "customer", "warehouse", "delivery_date", "status")
    list_filter = ("company", "delivery_date")
    search_fields = ("delivery_number", "customer__name_ar")


@admin.register(SalesInvoice)
class SalesInvoiceAdmin(admin.ModelAdmin):
    list_display = ("invoice_number", "company", "customer", "invoice_date", "grand_total", "status")
    list_filter = ("company", "status", "invoice_date")
    search_fields = ("invoice_number", "customer__name_ar")
    inlines = (SalesInvoiceLineInline,)


@admin.register(CustomerReceipt)
class CustomerReceiptAdmin(admin.ModelAdmin):
    list_display = ("receipt_number", "company", "customer", "invoice", "receipt_date", "amount", "status")
    list_filter = ("company", "status", "receipt_date", "method")
    search_fields = ("receipt_number", "customer__name_ar", "invoice__invoice_number")
