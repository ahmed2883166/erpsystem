"""تهيئة عرض الأصول الثابتة وجداول الإهلاك في لوحة Django الإدارية."""

from django.contrib import admin
from .models import FixedAsset, AssetDepreciation, AssetMovement

class AssetDepreciationInline(admin.TabularInline):
    model = AssetDepreciation
    extra = 0
    can_delete = False

@admin.register(FixedAsset)
class FixedAssetAdmin(admin.ModelAdmin):
    list_display = ("code", "name_ar", "company", "branch", "original_cost", "status")
    list_filter = ("company", "status", "depreciation_method")
    search_fields = ("code", "name_ar")
    inlines = (AssetDepreciationInline,)


@admin.register(AssetMovement)
class AssetMovementAdmin(admin.ModelAdmin):
    list_display = ("document_number", "asset", "movement_type", "movement_date", "from_branch", "to_branch", "status")
    list_filter = ("company", "movement_type", "status", "movement_date")
    search_fields = ("document_number", "asset__code", "asset__name_ar", "from_custodian", "to_custodian")
    readonly_fields = ("company", "asset", "movement_type", "movement_date", "from_branch", "to_branch", "from_employee", "to_employee", "from_custodian", "to_custodian", "from_location", "to_location", "document_number", "notes", "status", "requested_by", "reviewed_by", "reviewed_at", "review_note", "created_at")
