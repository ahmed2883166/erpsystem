from django.contrib import admin
from .models import Branch, Company, CompanyMembership, Currency, FiscalYear


@admin.register(CompanyMembership)
class CompanyMembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "company", "role", "is_active", "created_at")
    list_filter = ("company", "role", "is_active")
    search_fields = ("user__username", "user__display_name", "company__code", "company__name_ar")
    autocomplete_fields = ("user", "company")


@admin.register(Currency)
class CurrencyAdmin(admin.ModelAdmin):
    list_display = ("code", "name_ar", "symbol", "decimal_places", "is_active")
    search_fields = ("code", "name_ar", "name_en")
    list_filter = ("is_active",)


class BranchInline(admin.TabularInline):
    model = Branch
    extra = 0


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ("code", "name_ar", "country", "activity", "base_currency", "is_active")
    search_fields = ("code", "name_ar", "name_en")
    list_filter = ("country", "activity", "is_active")
    inlines = (BranchInline,)


@admin.register(FiscalYear)
class FiscalYearAdmin(admin.ModelAdmin):
    list_display = ("company", "name", "start_date", "end_date", "status")
    list_filter = ("company", "status")
