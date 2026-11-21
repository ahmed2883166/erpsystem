from rest_framework.routers import DefaultRouter

from .api import PurchaseInvoiceViewSet, PurchaseReceiptViewSet, SupplierPaymentViewSet

router = DefaultRouter()
router.register("receipts", PurchaseReceiptViewSet, basename="purchase-receipt")
router.register("invoices", PurchaseInvoiceViewSet, basename="purchase-invoice")
router.register("payments", SupplierPaymentViewSet, basename="supplier-payment")

urlpatterns = router.urls
