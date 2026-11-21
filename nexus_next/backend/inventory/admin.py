from django.contrib import admin

from .models import Product, ProductCategory, StorageLocation, UnitOfMeasure, Warehouse


@admin.register(ProductCategory, UnitOfMeasure, Product, Warehouse, StorageLocation)
class InventoryAdmin(admin.ModelAdmin):
    list_display = ("id",)
