from datetime import timedelta
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date
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


    @action(detail=False, methods=("get",), url_path="customer-aging")
    def customer_aging(self, request):
        try:
            company_id = int(request.query_params.get("company", ""))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة لعرض أعمار مديونية العملاء."})
        if not can_access_company(request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")

        as_of_raw = request.query_params.get("as_of")
        as_of = parse_date(as_of_raw) if as_of_raw else timezone.localdate()
        if as_of_raw and not as_of:
            raise ValidationError({"as_of": "صيغة التاريخ يجب أن تكون YYYY-MM-DD."})

        customer_id_raw = request.query_params.get("customer")
        customer_id = None
        if customer_id_raw:
            try:
                customer_id = int(customer_id_raw)
            except (TypeError, ValueError):
                raise ValidationError({"customer": "معرّف العميل غير صالح."})
            from customers.models import Customer
            if not Customer.objects.filter(pk=customer_id, company_id=company_id).exists():
                raise ValidationError({"customer": "العميل غير موجود في الشركة المحددة."})

        branch_id_raw = request.query_params.get("branch")
        branch_id = None
        if branch_id_raw:
            try:
                branch_id = int(branch_id_raw)
            except (TypeError, ValueError):
                raise ValidationError({"branch": "معرّف الفرع غير صالح."})
            if not can_access_branch(request.user, branch_id, company_id):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")

        invoices = self.get_queryset().filter(
            company_id=company_id,
            status=SalesInvoice.Status.POSTED,
            invoice_date__lte=as_of,
        ).select_related("customer", "delivery__warehouse").prefetch_related("receipts")
        if customer_id:
            invoices = invoices.filter(customer_id=customer_id)
        if branch_id:
            invoices = invoices.filter(delivery__warehouse__branch_id=branch_id)

        customers = {}
        totals = {
            "not_due": Decimal("0"), "1_30": Decimal("0"),
            "31_60": Decimal("0"), "61_90": Decimal("0"),
            "over_90": Decimal("0"), "total_due": Decimal("0"),
        }
        for invoice in invoices:
            paid_as_of = sum(
                (receipt.amount for receipt in invoice.receipts.all()
                 if receipt.status == CustomerReceipt.Status.POSTED
                 and receipt.receipt_date <= as_of),
                Decimal("0"),
            )
            balance_due = max(invoice.grand_total - paid_as_of, Decimal("0"))
            if balance_due <= 0:
                continue
            due_date = invoice.invoice_date + timedelta(days=invoice.customer.payment_terms_days)
            overdue_days = max((as_of - due_date).days, 0)
            bucket = (
                "not_due" if due_date >= as_of else
                "1_30" if overdue_days <= 30 else
                "31_60" if overdue_days <= 60 else
                "61_90" if overdue_days <= 90 else "over_90"
            )
            customer = customers.setdefault(invoice.customer_id, {
                "customer": invoice.customer_id,
                "customer_code": invoice.customer.code,
                "customer_name": invoice.customer.name_ar,
                "not_due": Decimal("0"), "1_30": Decimal("0"),
                "31_60": Decimal("0"), "61_90": Decimal("0"),
                "over_90": Decimal("0"), "total_due": Decimal("0"),
                "invoices": [],
            })
            customer[bucket] += balance_due
            customer["total_due"] += balance_due
            customer["invoices"].append({
                "invoice": invoice.pk,
                "invoice_number": invoice.invoice_number,
                "invoice_date": invoice.invoice_date.isoformat(),
                "due_date": due_date.isoformat(),
                "overdue_days": overdue_days,
                "bucket": bucket,
                "grand_total": str(invoice.grand_total),
                "paid_as_of": str(paid_as_of),
                "balance_due": str(balance_due),
            })
            totals[bucket] += balance_due
            totals["total_due"] += balance_due

        for customer in customers.values():
            for key in (*totals.keys(),):
                customer[key] = str(customer[key])
            customer["invoices"].sort(key=lambda row: (row["due_date"], row["invoice_number"]))
        return Response({
            "company": company_id,
            "as_of": as_of.isoformat(),
            "customer_filter": customer_id,
            "branch_filter": branch_id,
            "totals": {key: str(value) for key, value in totals.items()},
            "customers": sorted(customers.values(), key=lambda row: row["customer_name"]),
        })


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
