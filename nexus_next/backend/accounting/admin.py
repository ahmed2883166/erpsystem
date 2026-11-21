from django.contrib import admin
from .models import AccountRole, ChartOfAccount, CostCenter, JournalEntry, JournalLine, TaxCode


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

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status != JournalEntry.Status.DRAFT:
            return ("account", "description", "debit", "credit", "cost_center")
        return ()

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and (
            obj is None or obj.status == JournalEntry.Status.DRAFT
        )


@admin.register(JournalEntry)
class JournalEntryAdmin(admin.ModelAdmin):
    list_display = ("entry_number", "entry_date", "company", "branch", "source_type", "status")
    list_filter = ("company", "branch", "source_type", "status", "fiscal_year")
    search_fields = ("entry_number", "description")
    date_hierarchy = "entry_date"
    inlines = (JournalLineInline,)

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status != JournalEntry.Status.DRAFT:
            return ("company", "branch", "fiscal_year", "entry_number", "entry_date",
                    "description", "source_type", "status", "reversal_of",
                    "created_by", "created_at", "posted_at")
        return ("status", "posted_at", "created_at")

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and (
            obj is None or obj.status == JournalEntry.Status.DRAFT
        )
    readonly_fields = ("status", "posted_at", "created_at")



@admin.register(TaxCode)
class TaxCodeAdmin(admin.ModelAdmin):
    list_display = ("code", "name_ar", "company", "country", "category", "rate", "effective_from", "effective_to", "is_active")
    list_filter = ("company", "country", "category", "is_active", "input_tax_recoverable")
    search_fields = ("code", "name_ar")
    list_select_related = ("company",)
