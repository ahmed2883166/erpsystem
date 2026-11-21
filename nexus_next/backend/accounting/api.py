from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q, Sum
from django.utils.dateparse import parse_date
from rest_framework import decorators, permissions, response, status, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError

from companies.access import accessible_branches, accessible_companies, can_access_branch, can_access_company, has_company_role
from .models import AccountRole, ChartOfAccount, CostCenter, JournalEntry, JournalLine
from .permissions import CompanyRolePermission
from .serializers import AccountRoleSerializer, ChartOfAccountSerializer, CostCenterSerializer, JournalEntrySerializer
from .services import post_journal


class AccountingViewSet(viewsets.ModelViewSet):
    permission_classes = (CompanyRolePermission,)

    def get_queryset(self):
        if self.request.user.is_superuser:
            return self.queryset
        return self.queryset.filter(company__in=accessible_companies(self.request.user))

    def _validate_scope(self, data):
        company = data.get("company")
        if company and not can_access_company(self.request.user, company.pk):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        branch = data.get("branch")
        if branch and not can_access_branch(
            self.request.user, branch.pk, company.pk if company else None
        ):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")

    def _require_write_role(self, company_id, *, delete=False):
        roles = ("admin",) if delete else ("admin", "accountant")
        if not has_company_role(self.request.user, company_id, roles):
            raise PermissionDenied("تحتاج إلى دور مدير الشركة أو المحاسب لتنفيذ هذا الإجراء.")

    def perform_create(self, serializer):
        self._validate_scope(serializer.validated_data)
        self._require_write_role(serializer.validated_data["company"].pk)
        serializer.save()

    def perform_update(self, serializer):
        self._validate_scope(serializer.validated_data)
        company = serializer.validated_data.get("company", serializer.instance.company)
        self._require_write_role(company.pk)
        serializer.save()

    def perform_destroy(self, instance):
        self._require_write_role(instance.company_id, delete=True)
        instance.delete()


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

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.user.is_superuser:
            return qs
        return qs.filter(branch__in=accessible_branches(self.request.user))

    def perform_update(self, serializer):
        if serializer.instance.status != JournalEntry.Status.DRAFT:
            raise ValidationError("لا يمكن تعديل قيد مرحّل أو معكوس.")
        self._validate_scope(serializer.validated_data)
        serializer.save()

    @decorators.action(detail=True, methods=("post",))
    def post(self, request, pk=None):
        entry = self.get_object()
        self._require_write_role(entry.company_id)
        try:
            entry = post_journal(entry.pk)
        except DjangoValidationError as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
            raise ValidationError(detail) from exc
        return response.Response(self.get_serializer(entry).data, status=status.HTTP_200_OK)

    @decorators.action(detail=False, methods=("get",), url_path="trial-balance")
    def trial_balance(self, request):
        company_id = request.query_params.get("company")
        if not company_id or not company_id.isdigit():
            raise ValidationError({"company": "حدد الشركة لعرض ميزان المراجعة."})
        if not can_access_company(request.user, int(company_id)):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        if date_from and not parse_date(date_from):
            raise ValidationError({"date_from": "صيغة تاريخ البداية يجب أن تكون YYYY-MM-DD."})
        if date_to and not parse_date(date_to):
            raise ValidationError({"date_to": "صيغة تاريخ النهاية يجب أن تكون YYYY-MM-DD."})
        if date_from and date_to and date_from > date_to:
            raise ValidationError("تاريخ البداية يجب ألا يتجاوز تاريخ النهاية.")
        date_filter = Q(journal_lines__entry__status=JournalEntry.Status.POSTED)
        if not request.user.is_superuser:
            date_filter &= Q(
                journal_lines__entry__branch__in=accessible_branches(request.user, int(company_id))
            )
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


    @decorators.action(detail=False, methods=("get",), url_path="indirect-cash-flow")
    def indirect_cash_flow(self, request):
        from decimal import Decimal
        from django.db.models import Sum
        from assets.models import AssetDepreciation, FixedAsset
        from treasury.models import TreasuryAccount, TreasuryTransaction

        company_raw = request.query_params.get("company")
        date_from_raw = request.query_params.get("date_from")
        date_to_raw = request.query_params.get("date_to")
        if not company_raw or not company_raw.isdigit():
            raise ValidationError({"company": "حدد الشركة لعرض قائمة التدفقات النقدية."})
        company_id = int(company_raw)
        if not can_access_company(request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        date_from = parse_date(date_from_raw or "")
        date_to = parse_date(date_to_raw or "")
        if not date_from or not date_to:
            raise ValidationError({"date_from": "حدد تاريخ البداية والنهاية بصيغة YYYY-MM-DD."})
        if date_from > date_to:
            raise ValidationError("تاريخ البداية يجب ألا يتجاوز تاريخ النهاية.")
        branch_raw = request.query_params.get("branch")
        branch_id = None
        if branch_raw:
            if not branch_raw.isdigit():
                raise ValidationError({"branch": "رقم الفرع غير صحيح."})
            branch_id = int(branch_raw)
            if not can_access_branch(request.user, branch_id, company_id):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")

        visible_branches = accessible_branches(request.user, company_id)
        if branch_id:
            visible_branches = visible_branches.filter(pk=branch_id)
        entries = JournalEntry.objects.filter(
            company_id=company_id, status=JournalEntry.Status.POSTED,
            entry_date__gte=date_from, entry_date__lte=date_to,
            branch__in=visible_branches,
        )
        period_lines = JournalLine.objects.filter(entry__in=entries)
        profit_lines = period_lines.exclude(entry__source_type__in=(JournalEntry.Source.CLOSING, JournalEntry.Source.OPENING))

        revenue_totals = profit_lines.filter(account__nature=ChartOfAccount.Nature.REVENUE).aggregate(
            credit=Sum("credit"), debit=Sum("debit")
        )
        expense_totals = profit_lines.filter(account__nature=ChartOfAccount.Nature.EXPENSE).aggregate(
            debit=Sum("debit"), credit=Sum("credit")
        )
        net_profit = (
            (revenue_totals["credit"] or Decimal("0")) - (revenue_totals["debit"] or Decimal("0"))
            - (expense_totals["debit"] or Decimal("0")) + (expense_totals["credit"] or Decimal("0"))
        )

        role_codes = (
            AccountRole.Role.ACCOUNTS_RECEIVABLE, AccountRole.Role.INVENTORY,
            AccountRole.Role.VAT_INPUT, AccountRole.Role.ACCOUNTS_PAYABLE,
            AccountRole.Role.GOODS_RECEIVED_NOT_INVOICED, AccountRole.Role.VAT_OUTPUT,
        )
        role_accounts = {
            item.role: item.account
            for item in AccountRole.objects.filter(company_id=company_id, role__in=role_codes).select_related("account")
        }

        def account_balance(account, cutoff, inclusive=False):
            qs = account.journal_lines.filter(
                entry__status=JournalEntry.Status.POSTED,
                entry__company_id=company_id,
            )
            if inclusive:
                qs = qs.filter(entry__entry_date__lte=cutoff)
            else:
                qs = qs.filter(
                    Q(entry__entry_date__lt=cutoff)
                    | Q(entry__entry_date=cutoff, entry__source_type=JournalEntry.Source.OPENING)
                )
            qs = qs.filter(entry__branch__in=visible_branches)
            totals = qs.aggregate(debit=Sum("debit"), credit=Sum("credit"))
            debit = totals["debit"] or Decimal("0")
            credit = totals["credit"] or Decimal("0")
            return debit - credit if account.nature == ChartOfAccount.Nature.ASSET else credit - debit

        working_capital = []
        operating_adjustments = Decimal("0")
        missing_roles = []
        for role in role_codes:
            account = role_accounts.get(role)
            if not account:
                missing_roles.append(role)
                continue
            opening = account_balance(account, date_from, inclusive=False)
            closing = account_balance(account, date_to, inclusive=True)
            movement = closing - opening
            is_current_asset = role in (
                AccountRole.Role.ACCOUNTS_RECEIVABLE, AccountRole.Role.INVENTORY, AccountRole.Role.VAT_INPUT
            )
            adjustment = -movement if is_current_asset else movement
            operating_adjustments += adjustment
            working_capital.append({
                "role": role, "account_code": account.code, "account_name": account.name_ar,
                "opening_balance": opening, "closing_balance": closing,
                "change": movement, "cash_flow_adjustment": adjustment,
            })

        depreciation_qs = AssetDepreciation.objects.filter(
            asset__company_id=company_id, period_date__gte=date_from, period_date__lte=date_to,
            journal_entry__status=JournalEntry.Status.POSTED,
        )
        depreciation_qs = depreciation_qs.filter(asset__branch__in=visible_branches)
        depreciation_addback = depreciation_qs.aggregate(total=Sum("amount"))["total"] or Decimal("0")

        disposals = FixedAsset.objects.filter(
            company_id=company_id, status=FixedAsset.Status.DISPOSED,
            disposed_at__gte=date_from, disposed_at__lte=date_to,
            disposal_journal__status=JournalEntry.Status.POSTED,
        ).select_related("disposal_journal")
        disposals = disposals.filter(branch__in=visible_branches)
        disposal_ids = list(disposals.values_list("disposal_journal_id", flat=True))
        disposal_lines = JournalLine.objects.filter(
            entry_id__in=disposal_ids
        )
        disposal_gain = (
            (disposal_lines.filter(account__nature=ChartOfAccount.Nature.REVENUE).aggregate(v=Sum("credit"))["v"] or Decimal("0"))
            - (disposal_lines.filter(account__nature=ChartOfAccount.Nature.REVENUE).aggregate(v=Sum("debit"))["v"] or Decimal("0"))
        )
        disposal_loss = (
            (disposal_lines.filter(account__nature=ChartOfAccount.Nature.EXPENSE).aggregate(v=Sum("debit"))["v"] or Decimal("0"))
            - (disposal_lines.filter(account__nature=ChartOfAccount.Nature.EXPENSE).aggregate(v=Sum("credit"))["v"] or Decimal("0"))
        )
        non_cash_adjustments = depreciation_addback + disposal_loss - disposal_gain
        operating_cash_flow = net_profit + operating_adjustments + non_cash_adjustments

        treasury_rows = TreasuryTransaction.objects.filter(
            company_id=company_id, status=TreasuryTransaction.Status.POSTED,
            transaction_date__gte=date_from, transaction_date__lte=date_to,
        ).exclude(kind=TreasuryTransaction.Kind.TRANSFER)
        treasury_rows = treasury_rows.filter(branch__in=visible_branches)
        investing_rows = treasury_rows.filter(cash_flow_category=TreasuryTransaction.CashFlowCategory.INVESTING)
        financing_rows = treasury_rows.filter(cash_flow_category=TreasuryTransaction.CashFlowCategory.FINANCING)

        def cash_category_total(queryset):
            receipts = queryset.filter(kind=TreasuryTransaction.Kind.RECEIPT).aggregate(v=Sum("amount"))["v"] or Decimal("0")
            payments = queryset.filter(kind=TreasuryTransaction.Kind.PAYMENT).aggregate(v=Sum("amount"))["v"] or Decimal("0")
            return receipts - payments

        investing_cash_flow = cash_category_total(investing_rows)
        financing_cash_flow = cash_category_total(financing_rows)
        net_change = operating_cash_flow + investing_cash_flow + financing_cash_flow

        treasury_accounts = TreasuryAccount.objects.filter(company_id=company_id)
        from django.db.models import Q
        treasury_accounts = treasury_accounts.filter(Q(branch__isnull=True) | Q(branch__in=visible_branches))
        cash_account_ids = list(treasury_accounts.values_list("account_id", flat=True))
        cash_lines = JournalLine.objects.filter(
            account_id__in=cash_account_ids, entry__status=JournalEntry.Status.POSTED,
            entry__company_id=company_id, entry__entry_date__gte=date_from, entry__entry_date__lte=date_to,
        ).exclude(entry__source_type__in=(JournalEntry.Source.OPENING, JournalEntry.Source.CLOSING))
        cash_lines = cash_lines.filter(entry__branch__in=visible_branches)
        cash_totals = cash_lines.aggregate(debit=Sum("debit"), credit=Sum("credit"))
        ledger_cash_change = (cash_totals["debit"] or Decimal("0")) - (cash_totals["credit"] or Decimal("0"))

        return response.Response({
            "company": company_id, "branch": branch_id, "date_from": date_from, "date_to": date_to,
            "net_profit": net_profit,
            "non_cash_adjustments": {
                "depreciation": depreciation_addback, "disposal_loss": disposal_loss,
                "disposal_gain": disposal_gain, "net": non_cash_adjustments,
            },
            "working_capital": working_capital,
            "missing_account_roles": missing_roles,
            "operating_adjustments": operating_adjustments,
            "operating_cash_flow": operating_cash_flow,
            "investing_cash_flow": investing_cash_flow,
            "financing_cash_flow": financing_cash_flow,
            "net_change_in_cash": net_change,
            "ledger_cash_change": ledger_cash_change,
            "reconciliation_difference": ledger_cash_change - net_change,
            "investing_transactions": list(investing_rows.values(
                "id", "transaction_date", "document_number", "description", "kind", "amount"
            ).order_by("transaction_date", "id")),
            "financing_transactions": list(financing_rows.values(
                "id", "transaction_date", "document_number", "description", "kind", "amount"
            ).order_by("transaction_date", "id")),
            "note": "القائمة غير المباشرة تعتمد على أدوار الحسابات المرتبطة. اربط حسابات العملاء والمخزون والضريبة والموردين والبضاعة المستلمة غير المفوترة وضريبة المخرجات للحصول على تسوية مكتملة. فرق المطابقة يُعرض للمراجعة ولا يُرحّل تلقائيًا.",
        })

    @decorators.action(detail=True, methods=("get",))
    def totals(self, request, pk=None):
        entry = self.get_object()
        lines = list(entry.lines.all())
        debit = sum((line.debit for line in lines), start=0)
        credit = sum((line.credit for line in lines), start=0)
        return response.Response({"debit_total": debit, "credit_total": credit, "difference": debit - credit})
