from rest_framework.routers import DefaultRouter

from .api import DiscountApprovalPolicyViewSet, SalesInvoiceApprovalViewSet, FinancialApprovalRequestViewSet

router = DefaultRouter()
router.register("discount-policies", DiscountApprovalPolicyViewSet, basename="discount-approval-policy")
router.register("requests", SalesInvoiceApprovalViewSet, basename="sales-invoice-approval")
router.register("financial-requests", FinancialApprovalRequestViewSet, basename="financial-approval-request")

urlpatterns = router.urls
