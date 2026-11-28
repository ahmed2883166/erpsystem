from rest_framework.routers import DefaultRouter

from .api import BranchPurchasePolicyViewSet, LandedCostDocumentViewSet, PurchaseInvoiceViewSet, PurchaseOrderViewSet, PurchaseReceiptViewSet, PurchaseRequisitionViewSet, PurchaseRFQViewSet, PurchaseServiceAcceptanceViewSet, SupplierQuotationViewSet, SupplierPaymentViewSet, SupplierRefundViewSet, SupplierReturnViewSet

router = DefaultRouter()
router.register("branch-purchase-policies", BranchPurchasePolicyViewSet, basename="branch-purchase-policy")
router.register("orders", PurchaseOrderViewSet, basename="purchase-order")
router.register("requisitions", PurchaseRequisitionViewSet, basename="purchase-requisition")
router.register("rfqs", PurchaseRFQViewSet, basename="purchase-rfq")
router.register("quotations", SupplierQuotationViewSet, basename="supplier-quotation")
router.register("service-acceptances", PurchaseServiceAcceptanceViewSet, basename="purchase-service-acceptance")
router.register("receipts", PurchaseReceiptViewSet, basename="purchase-receipt")
router.register("invoices", PurchaseInvoiceViewSet, basename="purchase-invoice")
router.register("landed-costs", LandedCostDocumentViewSet, basename="landed-cost")
router.register("payments", SupplierPaymentViewSet, basename="supplier-payment")
router.register("returns", SupplierReturnViewSet, basename="supplier-return")
router.register("refunds", SupplierRefundViewSet, basename="supplier-refund")

urlpatterns = router.urls
