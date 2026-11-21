from django.urls import path
from rest_framework.routers import DefaultRouter
from .api import AccountRoleViewSet, AccountTemplateView, ChartOfAccountViewSet, CostCenterViewSet, JournalEntryViewSet, TaxCodeViewSet

router = DefaultRouter()
router.register("accounts", ChartOfAccountViewSet)
router.register("account-roles", AccountRoleViewSet)
router.register("cost-centers", CostCenterViewSet)
router.register("journal-entries", JournalEntryViewSet)
router.register("tax-codes", TaxCodeViewSet)

urlpatterns = [path("account-templates/", AccountTemplateView.as_view(), name="account-templates"), *router.urls]
