"""تهيئة عرض الأصول الثابتة وجداول الإهلاك في لوحة Django الإدارية."""

from django.contrib import admin
from .models import FixedAsset, AssetDepreciation

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
