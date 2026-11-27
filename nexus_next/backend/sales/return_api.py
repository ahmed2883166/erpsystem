from django.db import transaction
from django.db.models import Q
from django.utils import timezone, dateparse
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from companies.access import accessible_branches, accessible_companies, can_access_branch, is_platform_owner
from .api import SalesBaseViewSet, _document_template_snapshot, _sales_warehouse_accessible
from .models import SalesReturn, SalesDocumentTemplate
from .return_serializers import SalesReturnSerializer
from .return_services import post_sales_return, reverse_sales_return


class SalesReturnViewSet(SalesBaseViewSet):
    serializer_class = SalesReturnSerializer
    queryset = SalesReturn.objects.select_related(
        "company", "invoice", "invoice__customer", "invoice__delivery", "invoice__branch", "invoice__transaction_currency",
        "warehouse", "branch", "stock_movement", "journal_entry", "cost_journal_entry",
        "reversal_stock_movement", "reversal_journal_entry", "reversed_by",
    ).prefetch_related("lines__invoice_line__delivery_line__product", "lines__invoice_line__product")
    filterset_fields = ("company", "invoice", "status", "return_date")
    search_fields = ("return_number", "invoice__invoice_number", "invoice__customer__name_ar")
    ordering_fields = ("return_date", "return_number", "created_at")

    def get_queryset(self):
        qs = self.queryset.filter(company__in=accessible_companies(self.request.user))
        if self.request.user.is_superuser and not is_platform_owner(self.request.user):
            return qs
        branches = accessible_branches(self.request.user)
        default_company_ids = []
        for company_id in accessible_companies(self.request.user).values_list("id", flat=True):
            from companies.models import Branch
            default_branch = Branch.objects.filter(
                company_id=company_id, is_active=True
            ).order_by("-is_head_office", "code").first()
            if default_branch and can_access_branch(self.request.user, default_branch.pk, company_id):
                default_company_ids.append(company_id)
        return qs.filter(
            Q(warehouse__branch__in=branches)
            | Q(warehouse__isnull=False, warehouse__branch__isnull=True, company_id__in=default_company_ids)
            | Q(branch__in=branches)
        )

    def create(self, request, *args, **kwargs):
        try:
            self._check_write(int(request.data.get("company")))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة."}) from None
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        company_id = serializer.validated_data["company"].pk
        self._check_write(company_id)
        invoice = serializer.validated_data["invoice"]
        if invoice.delivery_id:
            accessible = _sales_warehouse_accessible(self.request.user, invoice.delivery.warehouse)
        else:
            accessible = bool(invoice.branch_id and can_access_branch(self.request.user, invoice.branch_id, invoice.company_id))
        if not accessible:
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الفاتورة الأصلية.")
        serializer.save()

    def perform_update(self, serializer):
        sales_return = serializer.instance
        self._check_write(sales_return.company_id)
        if sales_return.status != SalesReturn.Status.DRAFT:
            raise ValidationError("لا يمكن تعديل مردود مبيعات مرحّل.")
        if sales_return.warehouse_id:
            accessible = _sales_warehouse_accessible(self.request.user, sales_return.warehouse)
        else:
            accessible = bool(sales_return.branch_id and can_access_branch(self.request.user, sales_return.branch_id, sales_return.company_id))
        if not accessible:
            raise PermissionDenied("لا تملك صلاحية الوصول إلى مخزن المردود.")
        serializer.save()

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def post(self, request, pk=None):
        sales_return = self.get_object()
        self._check_write(sales_return.company_id)
        if sales_return.warehouse_id:
            accessible = _sales_warehouse_accessible(request.user, sales_return.warehouse)
        else:
            accessible = bool(sales_return.branch_id and can_access_branch(request.user, sales_return.branch_id, sales_return.company_id))
        if not accessible:
            raise PermissionDenied("لا تملك صلاحية الوصول إلى مخزن المردود.")
        sales_return.print_template_snapshot = _document_template_snapshot(
            sales_return.company, SalesDocumentTemplate.DocumentType.RETURN,
        )
        sales_return.save(update_fields=("print_template_snapshot",))
        sales_return = post_sales_return(sales_return.pk, request.user)
        return Response(self.get_serializer(sales_return).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=("post",))
    def reverse(self, request, pk=None):
        """عكس مردود المبيعات بقيد عكسي وحركة صرف من طبقات الاستلام الأصلية."""
        sales_return = self.get_object()
        self._check_write(sales_return.company_id)
        if sales_return.warehouse_id:
            accessible = _sales_warehouse_accessible(request.user, sales_return.warehouse)
        else:
            accessible = bool(sales_return.branch_id and can_access_branch(request.user, sales_return.branch_id, sales_return.company_id))
        if not accessible:
            raise PermissionDenied("لا تملك صلاحية الوصول إلى مخزن المردود.")
        raw_date = request.data.get("entry_date") or timezone.localdate().isoformat()
        entry_date = dateparse.parse_date(str(raw_date))
        if not entry_date:
            raise ValidationError({"entry_date": "أدخل تاريخ عكس صحيحًا بصيغة YYYY-MM-DD."})
        sales_return = reverse_sales_return(sales_return.pk, request.user, entry_date)
        return Response(self.get_serializer(sales_return).data, status=status.HTTP_200_OK)
