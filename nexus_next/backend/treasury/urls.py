from rest_framework.routers import DefaultRouter
from .api import TreasuryAccountViewSet, TreasuryTransactionViewSet, TreasuryChequeViewSet, BankReconciliationViewSet, BankStatementLineViewSet, CashCommitmentViewSet, CashCommitmentScheduleViewSet

router = DefaultRouter()
router.register("accounts", TreasuryAccountViewSet)
router.register("transactions", TreasuryTransactionViewSet)
router.register("cheques", TreasuryChequeViewSet)
router.register("reconciliations", BankReconciliationViewSet)
router.register("statement-lines", BankStatementLineViewSet)
router.register("commitments", CashCommitmentViewSet)
router.register("commitment-schedules", CashCommitmentScheduleViewSet)
urlpatterns = router.urls
