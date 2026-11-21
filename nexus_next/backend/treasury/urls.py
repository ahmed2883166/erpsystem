from rest_framework.routers import DefaultRouter
from .api import TreasuryAccountViewSet, TreasuryTransactionViewSet, BankReconciliationViewSet, BankStatementLineViewSet

router = DefaultRouter()
router.register("accounts", TreasuryAccountViewSet)
router.register("transactions", TreasuryTransactionViewSet)
router.register("reconciliations", BankReconciliationViewSet)
router.register("statement-lines", BankStatementLineViewSet)
urlpatterns = router.urls
