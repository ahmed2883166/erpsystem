from django.db.models import Q
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from companies.access import accessible_branches, accessible_companies, can_access_branch
from .api import SalesBaseViewSet, _sales_warehouse_accessible
from .models import SalesReturn
from .return_serializers import SalesReturnSerializer
from .return_services import post_sales_return


class SalesReturnViewSet(SalesBaseViewSet):
    serializer_class = SalesReturnSerializer
    queryset = SalesReturn.objects.select_related(
        "company", "invoice", "invoice__customer", "invoice__delivery",
        "warehouse", "stock_movement", "journal_entry", "cost_journal_entry",
    ).prefetch_related("lines__invoice_line__delivery_line__product")
    filterset_fields = ("company", "invoice", "status", "return_date")
    search_fields = ("return_number", "invoice__invoice_number", "invoice__customer__name_ar")
    ordering_fields = ("return_date", "return_number", "created_at")

    def get_queryset(self):
        qs = self.queryset.filter(company__in=accessible_companies(self.request.user))
        if self.request.user.is_superuser:
            return qs
        return qs.filter(
            Q(warehouse__branch__isnull=True)
            | Q(warehouse__branch__in=accessible_branches(self.request.user))
        )

    def create(self, request, *args, **kwargs):
        try:
            self._check_write(int(request.data.get("company")))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة."})
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        company_id = serializer.validated_data["company"].pk
        self._check_write(company_id)
        invoice = serializer.validated_data["invoice"]
        if not _sales_warehouse_accessible(self.request.user, invoice.delivery.warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع مخزن الفاتورة الأصلية.")
        serializer.save()

    def perform_update(self, serializer):
        sales_return = serializer.instance
        self._check_write(sales_return.company_id)
        if sales_return.status != SalesReturn.Status.DRAFT:
            raise ValidationError("لا يمكن تعديل مردود مبيعات مرحّل.")
        if not _sales_warehouse_accessible(self.request.user, sales_return.warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى مخزن المردود.")
        serializer.save()

    @action(detail=True, methods=("post",))
    def post(self, request, pk=None):
        sales_return = self.get_object()
        self._check_write(sales_return.company_id)
        if not _sales_warehouse_accessible(request.user, sales_return.warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى مخزن المردود.")
        sales_return = post_sales_return(sales_return.pk, request.user)
        return Response(self.get_serializer(sales_return).data, status=status.HTTP_200_OK)
