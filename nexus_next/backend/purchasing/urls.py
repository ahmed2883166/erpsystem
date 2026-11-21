from rest_framework.routers import DefaultRouter

from .api import PurchaseInvoiceViewSet, PurchaseReceiptViewSet

router = DefaultRouter()
router.register("receipts", PurchaseReceiptViewSet, basename="purchase-receipt")
router.register("invoices", PurchaseInvoiceViewSet, basename="purchase-invoice")

urlpatterns = router.urls
