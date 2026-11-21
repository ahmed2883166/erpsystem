from django.db.models import Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from accounting.permissions import CompanyRolePermission
from companies.access import accessible_branches, accessible_companies, can_access_branch, can_access_company, has_company_role
from inventory.models import Warehouse
from .models import PurchaseInvoice, PurchaseReceipt, SupplierPayment
from .serializers import PurchaseInvoiceSerializer, PurchaseReceiptSerializer, SupplierPaymentSerializer
from .services import post_purchase_invoice, post_purchase_receipt, post_supplier_payment


class PurchaseBaseViewSet(viewsets.ModelViewSet):
    permission_classes = (CompanyRolePermission,)

    def _check_write(self, company_id):
        if not can_access_company(self.request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        if not has_company_role(self.request.user, company_id, ("admin", "accountant")):
            raise PermissionDenied("إجراءات المشتريات متاحة لمدير الشركة والمحاسب فقط.")

    def perform_destroy(self, instance):
        raise PermissionDenied("لا يمكن حذف مستندات المشتريات؛ استخدم الإلغاء المعتمد بعد إضافته.")


class PurchaseReceiptViewSet(PurchaseBaseViewSet):
    serializer_class = PurchaseReceiptSerializer
    queryset = PurchaseReceipt.objects.select_related("company", "supplier", "warehouse", "stock_movement").prefetch_related("stock_movement__lines__product")
    filterset_fields = ("company", "supplier", "warehouse", "receipt_date")
    search_fields = ("receipt_number", "supplier_delivery_ref", "supplier__name_ar")
    ordering_fields = ("receipt_date", "receipt_number", "created_at")

    def get_queryset(self):
        qs = self.queryset.filter(company__in=accessible_companies(self.request.user))
        if self.request.user.is_superuser:
            return qs
        branches = accessible_branches(self.request.user)
        return qs.filter(Q(warehouse__branch__isnull=True) | Q(warehouse__branch__in=branches))

    def create(self, request, *args, **kwargs):
        try:
            company_id = int(request.data.get("company"))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة."})
        self._check_write(company_id)
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        data = serializer.validated_data
        self._check_write(data["company"].pk)
        warehouse = data["warehouse"]
        if warehouse.branch_id and not can_access_branch(self.request.user, warehouse.branch_id, data["company"].pk):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع المخزن.")
        serializer.save()

    @action(detail=True, methods=("post",))
    def post(self, request, pk=None):
        receipt = self.get_object()
        self._check_write(receipt.company_id)
        if receipt.warehouse.branch_id and not can_access_branch(request.user, receipt.warehouse.branch_id, receipt.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع المخزن.")
        receipt = post_purchase_receipt(receipt.pk, request.user)
        return Response(self.get_serializer(receipt).data, status=status.HTTP_200_OK)


class PurchaseInvoiceViewSet(PurchaseBaseViewSet):
    serializer_class = PurchaseInvoiceSerializer
    queryset = PurchaseInvoice.objects.select_related("company", "supplier", "receipt", "receipt__warehouse", "journal_entry").prefetch_related("lines__receipt_line__product")
    filterset_fields = ("company", "supplier", "status", "invoice_date")
    search_fields = ("invoice_number", "supplier__name_ar", "receipt__receipt_number")
    ordering_fields = ("invoice_date", "invoice_number", "created_at")

    def get_queryset(self):
        qs = self.queryset.filter(company__in=accessible_companies(self.request.user))
        if self.request.user.is_superuser:
            return qs
        branches = accessible_branches(self.request.user)
        return qs.filter(Q(receipt__warehouse__branch__isnull=True) | Q(receipt__warehouse__branch__in=branches))

    def create(self, request, *args, **kwargs):
        try:
            company_id = int(request.data.get("company"))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة."})
        self._check_write(company_id)
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        data = serializer.validated_data
        self._check_write(data["company"].pk)
        receipt = data["receipt"]
        if receipt.warehouse.branch_id and not can_access_branch(self.request.user, receipt.warehouse.branch_id, data["company"].pk):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع إذن الاستلام.")
        serializer.save()

    def perform_update(self, serializer):
        invoice = serializer.instance
        self._check_write(invoice.company_id)
        if invoice.status != PurchaseInvoice.Status.DRAFT:
            raise PermissionDenied("لا يمكن تعديل فاتورة مورد مرحلة.")
        raise ValidationError("تعديل الفاتورة غير متاح حاليًا؛ أنشئ فاتورة مسودة جديدة.")

    @action(detail=True, methods=("post",))
    def post(self, request, pk=None):
        invoice = self.get_object()
        self._check_write(invoice.company_id)
        if invoice.receipt.warehouse.branch_id and not can_access_branch(request.user, invoice.receipt.warehouse.branch_id, invoice.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع إذن الاستلام.")
        invoice = post_purchase_invoice(invoice.pk, request.user)
        return Response(self.get_serializer(invoice).data, status=status.HTTP_200_OK)



class SupplierPaymentViewSet(PurchaseBaseViewSet):
    serializer_class = SupplierPaymentSerializer
    queryset = SupplierPayment.objects.select_related(
        "company", "supplier", "invoice", "journal_entry"
    ).all()
    filterset_fields = ("company", "supplier", "invoice", "status", "payment_date", "method")
    search_fields = ("payment_number", "reference", "supplier__name_ar", "invoice__invoice_number")
    ordering_fields = ("payment_date", "payment_number", "amount", "created_at")

    def get_queryset(self):
        qs = self.queryset.filter(company__in=accessible_companies(self.request.user))
        if self.request.user.is_superuser:
            return qs
        branches = accessible_branches(self.request.user)
        return qs.filter(
            Q(invoice__receipt__warehouse__branch__isnull=True)
            | Q(invoice__receipt__warehouse__branch__in=branches)
        )

    def create(self, request, *args, **kwargs):
        try:
            company_id = int(request.data.get("company"))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة."})
        self._check_write(company_id)
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        data = serializer.validated_data
        self._check_write(data["company"].pk)
        invoice = data["invoice"]
        if invoice.receipt.warehouse.branch_id and not can_access_branch(
            self.request.user, invoice.receipt.warehouse.branch_id, data["company"].pk
        ):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الفاتورة.")
        serializer.save()

    def perform_update(self, serializer):
        self._check_write(serializer.instance.company_id)
        raise ValidationError("تعديل سند الصرف غير متاح؛ أنشئ سندًا جديدًا إذا كان مسودة.")

    @action(detail=True, methods=("post",))
    def post(self, request, pk=None):
        payment = self.get_object()
        self._check_write(payment.company_id)
        if payment.invoice.receipt.warehouse.branch_id and not can_access_branch(
            request.user, payment.invoice.receipt.warehouse.branch_id, payment.company_id
        ):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الفاتورة.")
        payment = post_supplier_payment(payment.pk, request.user)
        return Response(self.get_serializer(payment).data, status=status.HTTP_200_OK)
