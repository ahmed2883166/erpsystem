from rest_framework.routers import DefaultRouter

from .api import CustomerReceiptViewSet, SalesDeliveryViewSet, SalesInvoiceViewSet

router = DefaultRouter()
router.register("deliveries", SalesDeliveryViewSet, basename="sales-delivery")
router.register("invoices", SalesInvoiceViewSet, basename="sales-invoice")
router.register("receipts", CustomerReceiptViewSet, basename="customer-receipt")

urlpatterns = router.urls
