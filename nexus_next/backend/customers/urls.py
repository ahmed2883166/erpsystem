from rest_framework.routers import DefaultRouter

from .api import CustomerViewSet, CustomerDiscountRuleViewSet

router = DefaultRouter()
router.register("customers", CustomerViewSet, basename="customer")
router.register("customer-discount-rules", CustomerDiscountRuleViewSet, basename="customer-discount-rule")

urlpatterns = router.urls
