from rest_framework.routers import DefaultRouter

from .api import PurchaseInvoiceViewSet, PurchaseOrderViewSet, PurchaseReceiptViewSet, SupplierPaymentViewSet, SupplierReturnViewSet

router = DefaultRouter()
router.register("orders", PurchaseOrderViewSet, basename="purchase-order")
router.register("receipts", PurchaseReceiptViewSet, basename="purchase-receipt")
router.register("invoices", PurchaseInvoiceViewSet, basename="purchase-invoice")
router.register("payments", SupplierPaymentViewSet, basename="supplier-payment")
router.register("returns", SupplierReturnViewSet, basename="supplier-return")

urlpatterns = router.urls
