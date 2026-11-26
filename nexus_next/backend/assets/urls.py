from rest_framework.routers import DefaultRouter
from .api import FixedAssetViewSet
from .movement_api import AssetMovementViewSet

router = DefaultRouter()
router.register("fixed-assets", FixedAssetViewSet)
router.register("asset-movements", AssetMovementViewSet, basename="asset-movements")
urlpatterns = router.urls
