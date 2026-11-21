from rest_framework.routers import DefaultRouter
from .api import AccountRoleViewSet, ChartOfAccountViewSet, CostCenterViewSet, JournalEntryViewSet

router = DefaultRouter()
router.register("accounts", ChartOfAccountViewSet)
router.register("account-roles", AccountRoleViewSet)
router.register("cost-centers", CostCenterViewSet)
router.register("journal-entries", JournalEntryViewSet)

urlpatterns = router.urls
