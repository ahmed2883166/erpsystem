from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import decorators, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from companies.access import accessible_branches, accessible_companies, can_access_branch
from sales.api import SalesBaseViewSet
from sales.models import CustomerRefund
from sales.customer_refund_serializers import CustomerRefundSerializer
from sales.customer_refund_services import post_customer_refund, reverse_customer_refund


class CustomerRefundViewSet(SalesBaseViewSet):
    serializer_class = CustomerRefundSerializer
    queryset = CustomerRefund.objects.select_related(
        "company", "customer", "branch", "source_receipt_batch",
        "treasury_account__account", "treasury_transaction__journal_entry",
        "reversal_treasury_transaction", "reversal_journal_entry", "created_by",
    )
    filterset_fields = ("company", "customer", "branch", "source_receipt_batch", "status", "refund_date")
    search_fields = ("refund_number", "customer__name_ar", "source_receipt_batch__receipt_number")
    ordering_fields = ("refund_date", "refund_number", "amount", "created_at")

    def get_queryset(self):
        return self.queryset.filter(
            company__in=accessible_companies(self.request.user),
            branch__in=accessible_branches(self.request.user),
        )

    def create(self, request, *args, **kwargs):
        try:
            self._check_write(int(request.data.get("company")))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة."}) from None
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        data = serializer.validated_data
        self._check_write(data["company"].pk)
        if not can_access_branch(self.request.user, data["branch"].pk, data["company"].pk):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع مردود العميل.")
        serializer.save()

    def perform_update(self, serializer):
        self._check_write(serializer.instance.company_id)
        raise ValidationError("تعديل مردود العميل غير متاح؛ احذف المسودة وأنشئها من جديد.")

    def _check_refund_write_access(self, refund):
        self._check_write(refund.company_id)
        if not can_access_branch(self.request.user, refund.branch_id, refund.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع مردود العميل.")

    @decorators.action(detail=True, methods=("post",))
    @transaction.atomic
    def post(self, request, pk=None):
        refund = self.get_object()
        self._check_refund_write_access(refund)
        refund = post_customer_refund(refund.pk, request.user)
        return Response(self.get_serializer(refund).data, status=status.HTTP_200_OK)

    @decorators.action(detail=True, methods=("post",))
    @transaction.atomic
    def reverse(self, request, pk=None):
        refund = self.get_object()
        self._check_refund_write_access(refund)
        entry_date = parse_date(str(request.data.get("entry_date") or timezone.localdate()))
        if not entry_date:
            raise ValidationError({"entry_date": "صيغة تاريخ العكس يجب أن تكون YYYY-MM-DD."})
        refund = reverse_customer_refund(refund.pk, request.user, entry_date)
        return Response(self.get_serializer(refund).data, status=status.HTTP_200_OK)
