from rest_framework.routers import DefaultRouter
from .api import AccountRoleViewSet, ChartOfAccountViewSet, CostCenterViewSet, JournalEntryViewSet, TaxCodeViewSet

router = DefaultRouter()
router.register("accounts", ChartOfAccountViewSet)
router.register("account-roles", AccountRoleViewSet)
router.register("cost-centers", CostCenterViewSet)
router.register("journal-entries", JournalEntryViewSet)
router.register("tax-codes", TaxCodeViewSet)

urlpatterns = router.urls
