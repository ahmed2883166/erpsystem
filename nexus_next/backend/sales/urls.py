from rest_framework.routers import DefaultRouter

from .api import CustomerReceiptViewSet, SalesDeliveryViewSet, SalesInvoiceViewSet, SalesQuotationViewSet
from .return_api import SalesReturnViewSet

router = DefaultRouter()
router.register("quotations", SalesQuotationViewSet, basename="sales-quotation")
router.register("deliveries", SalesDeliveryViewSet, basename="sales-delivery")
router.register("invoices", SalesInvoiceViewSet, basename="sales-invoice")
router.register("receipts", CustomerReceiptViewSet, basename="customer-receipt")
router.register("returns", SalesReturnViewSet, basename="sales-return")

urlpatterns = router.urls
