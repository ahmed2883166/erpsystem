from rest_framework.routers import DefaultRouter
from .api import TreasuryAccountViewSet, TreasuryTransactionViewSet, TreasuryChequeViewSet, BankReconciliationViewSet, BankStatementLineViewSet, CashCommitmentViewSet, CashCommitmentScheduleViewSet, PettyCashFundViewSet, PettyCashExpenseViewSet

router = DefaultRouter()
router.register("accounts", TreasuryAccountViewSet)
router.register("transactions", TreasuryTransactionViewSet)
router.register("cheques", TreasuryChequeViewSet)
router.register("reconciliations", BankReconciliationViewSet)
router.register("statement-lines", BankStatementLineViewSet)
router.register("commitments", CashCommitmentViewSet)
router.register("commitment-schedules", CashCommitmentScheduleViewSet)
router.register("petty-cash-funds", PettyCashFundViewSet)
router.register("petty-cash-expenses", PettyCashExpenseViewSet)
urlpatterns = router.urls
