from rest_framework.routers import DefaultRouter

from .api import (
    ProductCategoryViewSet, ProductViewSet, StorageLocationViewSet,
    UnitOfMeasureViewSet, WarehouseViewSet,
)

router = DefaultRouter()
router.register("categories", ProductCategoryViewSet)
router.register("units", UnitOfMeasureViewSet)
router.register("products", ProductViewSet)
router.register("warehouses", WarehouseViewSet)
router.register("locations", StorageLocationViewSet)

urlpatterns = router.urls
