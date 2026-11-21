from rest_framework.routers import DefaultRouter
from .api import FixedAssetViewSet

router = DefaultRouter()
router.register("fixed-assets", FixedAssetViewSet)
urlpatterns = router.urls
