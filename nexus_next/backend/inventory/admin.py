from django.contrib import admin

from .models import (
    InventoryCount, InventoryCountLine, Product, ProductCategory, StorageLocation,
    UnitOfMeasure, Warehouse, WarehouseProductSetting,
)


@admin.register(ProductCategory, UnitOfMeasure, Product, Warehouse, StorageLocation, WarehouseProductSetting)
class InventoryAdmin(admin.ModelAdmin):
    list_display = ("id",)


@admin.register(InventoryCount)
class InventoryCountAdmin(admin.ModelAdmin):
    list_display = ("count_number", "company", "warehouse", "count_date", "status")
    list_filter = ("status", "count_date", "company")
    search_fields = ("count_number", "warehouse__name_ar")


@admin.register(InventoryCountLine)
class InventoryCountLineAdmin(admin.ModelAdmin):
    list_display = ("count", "product", "system_quantity", "counted_quantity", "adjustment_unit_cost")
    list_filter = ("count__status", "count__company")
    search_fields = ("product__code", "product__name_ar", "count__count_number")
