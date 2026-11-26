from rest_framework.routers import DefaultRouter
from .api import FixedAssetViewSet
from .movement_api import AssetMovementViewSet
from .control_api import AssetComponentViewSet, AssetInsurancePolicyViewSet, AssetPhysicalInventoryViewSet

router = DefaultRouter()
router.register("fixed-assets", FixedAssetViewSet)
router.register("asset-movements", AssetMovementViewSet, basename="asset-movements")
router.register("asset-components", AssetComponentViewSet, basename="asset-components")
router.register("asset-insurance", AssetInsurancePolicyViewSet, basename="asset-insurance")
router.register("asset-physical-inventory", AssetPhysicalInventoryViewSet, basename="asset-physical-inventory")
urlpatterns = router.urls
