from django.contrib import admin

from .models import Supplier


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ("code", "name_ar", "company", "tax_number", "is_active")
    list_filter = ("company", "supplier_type", "is_active")
    search_fields = ("code", "name_ar", "name_en", "tax_number")
