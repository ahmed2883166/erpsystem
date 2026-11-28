from rest_framework.routers import DefaultRouter

from .api import CustomerReceiptViewSet, SalesDeliveryViewSet, SalesInvoiceViewSet, SalesQuotationViewSet, SalesDocumentTemplateViewSet, SalesOrderViewSet
from .return_api import SalesReturnViewSet
from .receipt_batches_api import CustomerReceiptBatchViewSet
from .customer_refunds_api import CustomerRefundViewSet

router = DefaultRouter()
router.register("document-templates", SalesDocumentTemplateViewSet, basename="sales-document-template")
router.register("quotations", SalesQuotationViewSet, basename="sales-quotation")
router.register("orders", SalesOrderViewSet, basename="sales-order")
router.register("deliveries", SalesDeliveryViewSet, basename="sales-delivery")
router.register("invoices", SalesInvoiceViewSet, basename="sales-invoice")
router.register("receipts", CustomerReceiptViewSet, basename="customer-receipt")
router.register("receipt-batches", CustomerReceiptBatchViewSet, basename="customer-receipt-batch")
router.register("refunds", CustomerRefundViewSet, basename="customer-refund")
router.register("returns", SalesReturnViewSet, basename="sales-return")

urlpatterns = router.urls
