from rest_framework.routers import DefaultRouter
from .api import TreasuryAccountViewSet, TreasuryTransactionViewSet

router = DefaultRouter()
router.register("accounts", TreasuryAccountViewSet)
router.register("transactions", TreasuryTransactionViewSet)
urlpatterns = router.urls
