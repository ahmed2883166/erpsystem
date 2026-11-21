from django.contrib import admin
from .models import AccountRole, ChartOfAccount, CostCenter, JournalEntry, JournalLine


@admin.register(ChartOfAccount)
class ChartOfAccountAdmin(admin.ModelAdmin):
    list_display = ("code", "name_ar", "company", "nature", "is_group", "allow_posting", "is_active")
    list_filter = ("company", "nature", "is_group", "is_active")
    search_fields = ("code", "name_ar", "name_en")
    list_select_related = ("company", "parent")


@admin.register(AccountRole)
class AccountRoleAdmin(admin.ModelAdmin):
    list_display = ("company", "role", "account")
    list_filter = ("company", "role")


@admin.register(CostCenter)
class CostCenterAdmin(admin.ModelAdmin):
    list_display = ("code", "name_ar", "company", "is_active")
    list_filter = ("company", "is_active")
    search_fields = ("code", "name_ar")


class JournalLineInline(admin.TabularInline):
    model = JournalLine
    extra = 0
    autocomplete_fields = ("account", "cost_center")


@admin.register(JournalEntry)
class JournalEntryAdmin(admin.ModelAdmin):
    list_display = ("entry_number", "entry_date", "company", "branch", "source_type", "status")
    list_filter = ("company", "branch", "source_type", "status", "fiscal_year")
    search_fields = ("entry_number", "description")
    date_hierarchy = "entry_date"
    inlines = (JournalLineInline,)
    readonly_fields = ("status", "posted_at", "created_at")
