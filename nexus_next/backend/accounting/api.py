from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q, Sum
from django.utils.dateparse import parse_date
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

    @decorators.action(detail=False, methods=("get",), url_path="trial-balance")
    def trial_balance(self, request):
        company_id = request.query_params.get("company")
        if not company_id or not company_id.isdigit():
            raise ValidationError({"company": "حدد الشركة لعرض ميزان المراجعة."})
        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        if date_from and not parse_date(date_from):
            raise ValidationError({"date_from": "صيغة تاريخ البداية يجب أن تكون YYYY-MM-DD."})
        if date_to and not parse_date(date_to):
            raise ValidationError({"date_to": "صيغة تاريخ النهاية يجب أن تكون YYYY-MM-DD."})
        if date_from and date_to and date_from > date_to:
            raise ValidationError("تاريخ البداية يجب ألا يتجاوز تاريخ النهاية.")
        date_filter = Q(journal_lines__entry__status=JournalEntry.Status.POSTED)
        if date_from:
            date_filter &= Q(journal_lines__entry__entry_date__gte=date_from)
        if date_to:
            date_filter &= Q(journal_lines__entry__entry_date__lte=date_to)
        accounts = ChartOfAccount.objects.filter(company_id=company_id).annotate(
            debit_total=Sum("journal_lines__debit", filter=date_filter),
            credit_total=Sum("journal_lines__credit", filter=date_filter),
        ).order_by("code")
        rows = []
        debit_sum = 0
        credit_sum = 0
        for account in accounts:
            debit = account.debit_total or 0
            credit = account.credit_total or 0
            debit_sum += debit
            credit_sum += credit
            rows.append({
                "account_id": account.pk,
                "code": account.code,
                "name_ar": account.name_ar,
                "nature": account.nature,
                "is_group": account.is_group,
                "debit_total": debit,
                "credit_total": credit,
                "net_balance": debit - credit,
            })
        return response.Response({
            "company_id": int(company_id),
            "date_from": date_from,
            "date_to": date_to,
            "accounts": rows,
            "debit_total": debit_sum,
            "credit_total": credit_sum,
            "difference": debit_sum - credit_sum,
        })

    @decorators.action(detail=True, methods=("get",))
    def totals(self, request, pk=None):
        entry = self.get_object()
        lines = list(entry.lines.all())
        debit = sum((line.debit for line in lines), start=0)
        credit = sum((line.credit for line in lines), start=0)
        return response.Response({"debit_total": debit, "credit_total": credit, "difference": debit - credit})
