from django.contrib import admin

from .models import Customer


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("code", "name_ar", "company", "customer_type", "phone", "credit_limit", "is_active")
    list_filter = ("company", "customer_type", "is_active")
    search_fields = ("code", "name_ar", "name_en", "tax_number", "phone", "mobile", "email")
    readonly_fields = ("created_at", "updated_at")
