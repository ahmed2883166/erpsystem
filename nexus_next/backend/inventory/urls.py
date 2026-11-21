from rest_framework.routers import DefaultRouter

from .api import (
    ProductCategoryViewSet, ProductViewSet, StockBalanceViewSet, StockMovementViewSet, StorageLocationViewSet, WarehouseProductSettingViewSet,
    UnitOfMeasureViewSet, WarehouseViewSet,
)

router = DefaultRouter()
router.register("categories", ProductCategoryViewSet)
router.register("units", UnitOfMeasureViewSet)
router.register("products", ProductViewSet)
router.register("warehouses", WarehouseViewSet)
router.register("warehouse-product-settings", WarehouseProductSettingViewSet)
router.register("locations", StorageLocationViewSet)
router.register("movements", StockMovementViewSet)
router.register("balances", StockBalanceViewSet)

urlpatterns = router.urls
