from django.contrib import admin
from companies.access import is_platform_owner
from .models import Branch, Company, CompanyMembership, Currency, ExchangeRate, FiscalYear


@admin.register(Currency)
class CurrencyAdmin(admin.ModelAdmin):
    list_display = ("code", "name_ar", "symbol", "decimal_places", "is_active")
    search_fields = ("code", "name_ar", "name_en")
    list_filter = ("is_active",)


class BranchInline(admin.TabularInline):
    model = Branch
    extra = 0


class TenantModelAdmin(admin.ModelAdmin):
    """Keep platform-owner identities out of tenant-data administration."""

    def _allowed(self, request):
        return not is_platform_owner(request.user)

    def has_module_permission(self, request):
        return self._allowed(request) and super().has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        return self._allowed(request) and super().has_view_permission(request, obj)

    def has_add_permission(self, request):
        return self._allowed(request) and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        return self._allowed(request) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self._allowed(request) and super().has_delete_permission(request, obj)


@admin.register(Company)
class CompanyAdmin(TenantModelAdmin):
    list_display = ("code", "name_ar", "country", "activity", "base_currency", "is_active")
    search_fields = ("code", "name_ar", "name_en")
    list_filter = ("country", "activity", "is_active")
    inlines = (BranchInline,)


@admin.register(CompanyMembership)
class CompanyMembershipAdmin(TenantModelAdmin):
    list_display = ("user", "company", "role", "all_branches", "is_active")
    list_filter = ("company", "role", "all_branches", "is_active")
    search_fields = ("user__username", "user__display_name", "company__code", "company__name_ar")
    autocomplete_fields = ("user", "company")
    filter_horizontal = ("branches",)
    readonly_fields = ("user", "company", "role", "all_branches", "branches", "is_active", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(FiscalYear)
class FiscalYearAdmin(TenantModelAdmin):
    list_display = ("company", "name", "start_date", "end_date", "status")
    list_filter = ("company", "status")


@admin.register(ExchangeRate)
class ExchangeRateAdmin(TenantModelAdmin):
    list_display = ("company", "currency", "rate_date", "rate", "source")
    list_filter = ("company", "currency", "rate_date")
    search_fields = ("company__code", "currency__code", "source")
    readonly_fields = ("created_at",)
