from django.db.models import Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from accounting.permissions import CompanyRolePermission
from companies.access import accessible_branches, accessible_companies, can_access_branch, can_access_company, has_company_role
from .models import CustomerReceipt, SalesDelivery, SalesInvoice
from .serializers import CustomerReceiptSerializer, SalesDeliverySerializer, SalesInvoiceSerializer
from .services import post_customer_receipt, post_sales_delivery, post_sales_invoice


class SalesBaseViewSet(viewsets.ModelViewSet):
    permission_classes = (CompanyRolePermission,)

    def _check_write(self, company_id):
        if not can_access_company(self.request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        if not has_company_role(self.request.user, company_id, ("admin", "accountant")):
            raise PermissionDenied("إجراءات المبيعات متاحة لمدير الشركة والمحاسب فقط.")

    def perform_destroy(self, instance):
        raise PermissionDenied("لا يمكن حذف مستندات المبيعات؛ استخدم الإلغاء المعتمد بعد إضافته.")


class SalesDeliveryViewSet(SalesBaseViewSet):
    serializer_class = SalesDeliverySerializer
    queryset = SalesDelivery.objects.select_related("company", "customer", "warehouse", "stock_movement").prefetch_related("stock_movement__lines__product")
    filterset_fields = ("company", "customer", "warehouse", "delivery_date")
    search_fields = ("delivery_number", "customer_reference", "customer__name_ar")
    ordering_fields = ("delivery_date", "delivery_number", "created_at")

    def get_queryset(self):
        qs = self.queryset.filter(company__in=accessible_companies(self.request.user))
        if self.request.user.is_superuser:
            return qs
        return qs.filter(Q(warehouse__branch__isnull=True) | Q(warehouse__branch__in=accessible_branches(self.request.user)))

    def create(self, request, *args, **kwargs):
        try:
            self._check_write(int(request.data.get("company")))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة."})
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        data = serializer.validated_data
        self._check_write(data["company"].pk)
        if data["warehouse"].branch_id and not can_access_branch(self.request.user, data["warehouse"].branch_id, data["company"].pk):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع المخزن.")
        serializer.save()

    def perform_update(self, serializer):
        self._check_write(serializer.instance.company_id)
        raise ValidationError("لا يمكن تعديل إذن التسليم بعد إنشائه؛ أنشئ إذنًا جديدًا قبل الترحيل.")

    @action(detail=True, methods=("post",))
    def post(self, request, pk=None):
        delivery = self.get_object()
        self._check_write(delivery.company_id)
        if delivery.warehouse.branch_id and not can_access_branch(request.user, delivery.warehouse.branch_id, delivery.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع المخزن.")
        delivery = post_sales_delivery(delivery.pk, request.user)
        return Response(self.get_serializer(delivery).data, status=status.HTTP_200_OK)


class SalesInvoiceViewSet(SalesBaseViewSet):
    serializer_class = SalesInvoiceSerializer
    queryset = SalesInvoice.objects.select_related("company", "customer", "delivery", "delivery__warehouse", "journal_entry").prefetch_related("lines__delivery_line__product", "receipts")
    filterset_fields = ("company", "customer", "status", "invoice_date")
    search_fields = ("invoice_number", "customer__name_ar", "delivery__delivery_number")
    ordering_fields = ("invoice_date", "invoice_number", "created_at")

    def get_queryset(self):
        qs = self.queryset.filter(company__in=accessible_companies(self.request.user))
        if self.request.user.is_superuser:
            return qs
        return qs.filter(Q(delivery__warehouse__branch__isnull=True) | Q(delivery__warehouse__branch__in=accessible_branches(self.request.user)))

    def create(self, request, *args, **kwargs):
        try:
            self._check_write(int(request.data.get("company")))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة."})
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        data = serializer.validated_data
        self._check_write(data["company"].pk)
        warehouse = data["delivery"].warehouse
        if warehouse.branch_id and not can_access_branch(self.request.user, warehouse.branch_id, data["company"].pk):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع إذن التسليم.")
        serializer.save()

    def perform_update(self, serializer):
        self._check_write(serializer.instance.company_id)
        raise ValidationError("تعديل فاتورة المبيعات غير متاح؛ أنشئ مستندًا جديدًا إذا كانت مسودة.")

    @action(detail=True, methods=("post",))
    def post(self, request, pk=None):
        invoice = self.get_object()
        self._check_write(invoice.company_id)
        warehouse = invoice.delivery.warehouse
        if warehouse.branch_id and not can_access_branch(request.user, warehouse.branch_id, invoice.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع إذن التسليم.")
        invoice = post_sales_invoice(invoice.pk, request.user)
        return Response(self.get_serializer(invoice).data, status=status.HTTP_200_OK)


class CustomerReceiptViewSet(SalesBaseViewSet):
    serializer_class = CustomerReceiptSerializer
    queryset = CustomerReceipt.objects.select_related("company", "customer", "invoice", "journal_entry")
    filterset_fields = ("company", "customer", "invoice", "status", "receipt_date", "method")
    search_fields = ("receipt_number", "reference", "customer__name_ar", "invoice__invoice_number")
    ordering_fields = ("receipt_date", "receipt_number", "amount", "created_at")

    def get_queryset(self):
        qs = self.queryset.filter(company__in=accessible_companies(self.request.user))
        if self.request.user.is_superuser:
            return qs
        return qs.filter(Q(invoice__delivery__warehouse__branch__isnull=True) | Q(invoice__delivery__warehouse__branch__in=accessible_branches(self.request.user)))

    def create(self, request, *args, **kwargs):
        try:
            self._check_write(int(request.data.get("company")))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة."})
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        data = serializer.validated_data
        self._check_write(data["company"].pk)
        warehouse = data["invoice"].delivery.warehouse
        if warehouse.branch_id and not can_access_branch(self.request.user, warehouse.branch_id, data["company"].pk):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الفاتورة.")
        serializer.save()

    def perform_update(self, serializer):
        self._check_write(serializer.instance.company_id)
        raise ValidationError("تعديل سند القبض غير متاح؛ أنشئ سندًا جديدًا إذا كان مسودة.")

    def perform_update(self, serializer):
        self._check_write(serializer.instance.company_id)
        raise ValidationError("تعديل سند القبض غير متاح؛ أنشئ سندًا جديدًا إذا كان مسودة.")

    @action(detail=True, methods=("post",))
    def post(self, request, pk=None):
        receipt = self.get_object()
        self._check_write(receipt.company_id)
        warehouse = receipt.invoice.delivery.warehouse
        if warehouse.branch_id and not can_access_branch(request.user, warehouse.branch_id, receipt.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الفاتورة.")
        receipt = post_customer_receipt(receipt.pk, request.user)
        return Response(self.get_serializer(receipt).data, status=status.HTTP_200_OK)
