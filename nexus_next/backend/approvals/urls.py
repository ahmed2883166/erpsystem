from rest_framework.routers import DefaultRouter

from .api import DiscountApprovalPolicyViewSet, SalesInvoiceApprovalViewSet

router = DefaultRouter()
router.register("discount-policies", DiscountApprovalPolicyViewSet, basename="discount-approval-policy")
router.register("requests", SalesInvoiceApprovalViewSet, basename="sales-invoice-approval")

urlpatterns = router.urls
