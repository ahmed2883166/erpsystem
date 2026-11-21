from rest_framework.routers import DefaultRouter

from .api import (
    ProductCategoryViewSet, ProductViewSet, StockMovementViewSet, StorageLocationViewSet,
    UnitOfMeasureViewSet, WarehouseViewSet,
)

router = DefaultRouter()
router.register("categories", ProductCategoryViewSet)
router.register("units", UnitOfMeasureViewSet)
router.register("products", ProductViewSet)
router.register("warehouses", WarehouseViewSet)
router.register("locations", StorageLocationViewSet)
router.register("movements", StockMovementViewSet)

urlpatterns = router.urls
