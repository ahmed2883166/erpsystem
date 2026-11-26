from django.contrib import admin

from .models import CustomerReceipt, CustomerReceiptBatch, CustomerReceiptBatchAllocation, SalesDelivery, SalesInvoice, SalesInvoiceLine


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


@admin.register(CustomerReceiptBatch)
class CustomerReceiptBatchAdmin(admin.ModelAdmin):
    list_display = ("receipt_number", "company", "customer", "branch", "receipt_date", "amount", "status")
    list_filter = ("company", "branch", "status", "receipt_date", "method")
    search_fields = ("receipt_number", "customer__name_ar", "reference")
    readonly_fields = tuple(field.name for field in CustomerReceiptBatch._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CustomerReceiptBatchAllocation)
class CustomerReceiptBatchAllocationAdmin(admin.ModelAdmin):
    list_display = ("batch", "invoice", "amount", "created_at")
    list_filter = ("batch__company", "batch__receipt_date")
    search_fields = ("batch__receipt_number", "invoice__invoice_number")
    readonly_fields = tuple(field.name for field in CustomerReceiptBatchAllocation._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
