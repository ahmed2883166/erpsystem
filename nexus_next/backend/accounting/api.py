from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import decorators, permissions, response, status, viewsets
from rest_framework.exceptions import ValidationError

from .models import AccountRole, ChartOfAccount, CostCenter, JournalEntry
from .serializers import AccountRoleSerializer, ChartOfAccountSerializer, CostCenterSerializer, JournalEntrySerializer
from .services import post_journal


class AccountingViewSet(viewsets.ModelViewSet):
    permission_classes = (permissions.DjangoModelPermissions,)


class ChartOfAccountViewSet(AccountingViewSet):
    serializer_class = ChartOfAccountSerializer
    queryset = ChartOfAccount.objects.select_related("company", "parent").all()
    filterset_fields = ("company", "nature", "is_group", "is_active")
    search_fields = ("code", "name_ar", "name_en")
    ordering_fields = ("code", "name_ar")


class AccountRoleViewSet(AccountingViewSet):
    serializer_class = AccountRoleSerializer
    queryset = AccountRole.objects.select_related("company", "account").all()
    filterset_fields = ("company", "role")


class CostCenterViewSet(AccountingViewSet):
    serializer_class = CostCenterSerializer
    queryset = CostCenter.objects.select_related("company", "parent").all()
    filterset_fields = ("company", "is_active")
    search_fields = ("code", "name_ar")


class JournalEntryViewSet(AccountingViewSet):
    serializer_class = JournalEntrySerializer
    queryset = JournalEntry.objects.select_related(
        "company", "branch", "fiscal_year", "created_by"
    ).prefetch_related("lines__account", "lines__cost_center").all()
    filterset_fields = ("company", "branch", "fiscal_year", "status", "source_type")
    search_fields = ("entry_number", "description")
    ordering_fields = ("entry_date", "entry_number", "created_at")

    def perform_update(self, serializer):
        if serializer.instance.status != JournalEntry.Status.DRAFT:
            raise ValidationError("لا يمكن تعديل قيد مرحّل أو معكوس.")
        serializer.save()

    @decorators.action(detail=True, methods=("post",))
    def post(self, request, pk=None):
        try:
            entry = post_journal(int(pk))
        except DjangoValidationError as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
            raise ValidationError(detail) from exc
        return response.Response(self.get_serializer(entry).data, status=status.HTTP_200_OK)

    @decorators.action(detail=True, methods=("get",))
    def totals(self, request, pk=None):
        entry = self.get_object()
        lines = list(entry.lines.all())
        debit = sum((line.debit for line in lines), start=0)
        credit = sum((line.credit for line in lines), start=0)
        return response.Response({"debit_total": debit, "credit_total": credit, "difference": debit - credit})
