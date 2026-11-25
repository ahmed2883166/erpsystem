from django.urls import path
from rest_framework.routers import DefaultRouter
from .api import AccountRoleViewSet, AccountTemplateView, AccountImportView, ChartOfAccountViewSet, CostCenterViewSet, JournalEntryViewSet, TaxCodeViewSet
from .reconciliation import SubledgerReconciliationView

router = DefaultRouter()
router.register("accounts", ChartOfAccountViewSet)
router.register("account-roles", AccountRoleViewSet)
router.register("cost-centers", CostCenterViewSet)
router.register("journal-entries", JournalEntryViewSet)
router.register("tax-codes", TaxCodeViewSet)

urlpatterns = [path("subledger-reconciliation/", SubledgerReconciliationView.as_view(), name="subledger-reconciliation"), path("account-templates/", AccountTemplateView.as_view(), name="account-templates"), path("account-import/", AccountImportView.as_view(), name="account-import"), *router.urls]
