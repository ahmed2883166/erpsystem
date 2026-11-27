from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import decorators, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from companies.access import accessible_branches, accessible_companies, can_access_branch
from sales.api import SalesBaseViewSet, _document_template_snapshot
from sales.models import CustomerReceiptBatch, SalesDocumentTemplate
from sales.receipt_batch_serializers import CustomerReceiptBatchSerializer
from sales.receipt_batch_services import (
    allocate_customer_receipt_batch, post_customer_receipt_batch,
    reverse_customer_receipt_batch,
)


class CustomerReceiptBatchViewSet(SalesBaseViewSet):
    serializer_class = CustomerReceiptBatchSerializer
    queryset = CustomerReceiptBatch.objects.select_related(
        "company", "customer", "branch", "payment_account",
        "journal_entry", "reversal_journal_entry",
    ).prefetch_related("allocations__invoice")

    filterset_fields = ("company", "customer", "branch", "status", "receipt_date", "method")
    search_fields = ("receipt_number", "reference", "customer__name_ar")
    ordering_fields = ("receipt_date", "receipt_number", "amount", "created_at")

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
        company = data["company"]
        branch = data["branch"]
        self._check_write(company.pk)
        if not can_access_branch(self.request.user, branch.pk, company.pk):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع سند القبض.")
        serializer.save()

    def perform_update(self, serializer):
        self._check_write(serializer.instance.company_id)
        raise ValidationError("تعديل سند القبض المجمع غير متاح؛ احذف المسودة وأنشئها من جديد.")

    def _check_batch_write_access(self, batch):
        self._check_write(batch.company_id)
        if not can_access_branch(self.request.user, batch.branch_id, batch.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع سند القبض.")

    @decorators.action(detail=True, methods=("post",))
    @transaction.atomic
    def post(self, request, pk=None):
        batch = self.get_object()
        self._check_batch_write_access(batch)
        batch.print_template_snapshot = _document_template_snapshot(
            batch.company, SalesDocumentTemplate.DocumentType.RECEIPT,
        )
        batch.save(update_fields=("print_template_snapshot",))
        batch = post_customer_receipt_batch(batch.pk, request.user)
        return Response(self.get_serializer(batch).data, status=status.HTTP_200_OK)

    @decorators.action(detail=True, methods=("post",))
    @transaction.atomic
    def allocate(self, request, pk=None):
        batch = self.get_object()
        self._check_batch_write_access(batch)
        try:
            invoice_id = int(request.data.get("invoice"))
            amount = Decimal(str(request.data.get("amount")))
        except (TypeError, ValueError, InvalidOperation):
            raise ValidationError({"allocation": "حدد الفاتورة ومبلغ التخصيص الصحيح."}) from None
        raw_allocation_date = request.data.get("allocation_date")
        allocation_date = parse_date(str(raw_allocation_date)) if raw_allocation_date else timezone.localdate()
        if not allocation_date:
            raise ValidationError({"allocation_date": "صيغة تاريخ التخصيص يجب أن تكون YYYY-MM-DD."})
        allocate_customer_receipt_batch(
            batch.pk, invoice_id, amount, request.user, allocation_date=allocation_date,
        )
        batch.refresh_from_db()
        return Response(self.get_serializer(batch).data, status=status.HTTP_200_OK)

    @decorators.action(detail=True, methods=("post",))
    @transaction.atomic
    def reverse(self, request, pk=None):
        batch = self.get_object()
        self._check_batch_write_access(batch)
        raw_date = request.data.get("entry_date")
        entry_date = timezone.localdate()
        if raw_date:
            from django.utils.dateparse import parse_date
            entry_date = parse_date(str(raw_date))
            if not entry_date:
                raise ValidationError({"entry_date": "صيغة تاريخ العكس يجب أن تكون YYYY-MM-DD."})
        batch = reverse_customer_receipt_batch(batch.pk, request.user, entry_date)
        return Response(self.get_serializer(batch).data, status=status.HTTP_200_OK)
