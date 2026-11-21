from django.contrib import admin

from .models import Product, ProductCategory, StorageLocation, UnitOfMeasure, Warehouse, WarehouseProductSetting


@admin.register(ProductCategory, UnitOfMeasure, Product, Warehouse, StorageLocation, WarehouseProductSetting)
class InventoryAdmin(admin.ModelAdmin):
    list_display = ("id",)
