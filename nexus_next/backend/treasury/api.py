import csv
import io
from datetime import date, datetime
from django.http import Http404
from decimal import Decimal, InvalidOperation
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone, dateparse
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import decorators, permissions, response, status, viewsets
from django.db import models
from rest_framework.exceptions import PermissionDenied, ValidationError

from companies.access import accessible_branches, accessible_companies, can_access_branch, can_access_company, has_company_role, has_company_wide_branch_access, is_platform_owner
from .models import TreasuryAccount, TreasuryTransaction, TreasuryFxRevaluation, BankReconciliation, BankStatementLine
from .serializers import TreasuryAccountSerializer, TreasuryTransactionSerializer, BankReconciliationSerializer, BankStatementLineSerializer
from .services import (
    post_treasury_transaction, reverse_treasury_transaction, revalue_treasury_fx_account,
    reverse_treasury_fx_revaluation, can_reverse_treasury_fx_revaluation,
)
from approvals.models import FinancialApprovalRequest
from approvals.services import refresh_financial_approval


def _signed_bank_transaction_amount(doc, bank_account):
    """قيمة حركة البنك بعملة الحساب؛ الأجنبية اسمية وليست القيمة الوظيفية."""
    foreign = bank_account.currency_code != bank_account.company.base_currency.code
    value = doc.transaction_amount if foreign else doc.amount
    if value is None:
        raise ValidationError("الحركة الأجنبية لا تحتوي على مبلغ اسمي صالح للمطابقة.")
    if doc.source_account_id == bank_account.pk:
        return value if doc.kind == TreasuryTransaction.Kind.RECEIPT else -value
    if doc.destination_account_id == bank_account.pk and doc.kind == TreasuryTransaction.Kind.TRANSFER:
        return value
    raise ValidationError("الحركة لا تخص الحساب البنكي محل التسوية.")


def _bank_book_balance(bank_account, statement_date, branch_ids):
    """الرصيد الدفتري بوحدة الحساب: اسمي للأجنبي ووظيفي للعملة الأساسية."""
    if bank_account.currency_code != bank_account.company.base_currency.code:
        docs = TreasuryTransaction.objects.filter(
            company_id=bank_account.company_id, status=TreasuryTransaction.Status.POSTED,
            transaction_date__lte=statement_date, branch_id__in=branch_ids,
        ).filter(models.Q(source_account=bank_account) | models.Q(destination_account=bank_account))
        return sum((_signed_bank_transaction_amount(doc, bank_account) for doc in docs), Decimal("0"))
    book_lines = bank_account.account.journal_lines.filter(
        entry__status__in=("posted", "reversed"),
        entry__entry_date__lte=statement_date, entry__branch_id__in=branch_ids,
    )
    totals = book_lines.aggregate(debit=Sum("debit"), credit=Sum("credit"))
    opening_base = Decimal("0") if bank_account.opening_journal_entry_id else bank_account.opening_balance
    return opening_base + (totals["debit"] or Decimal("0")) - (totals["credit"] or Decimal("0"))


class TreasuryBaseViewSet(viewsets.ModelViewSet):
    permission_classes = (permissions.IsAuthenticated,)
    def _write(self, company_id):
        """التحقق من صلاحية الكتابة على حسابات وحركات الخزينة."""
        if not has_company_role(self.request.user, company_id, ("admin", "accountant")):
            raise PermissionDenied("تحتاج إلى دور مدير الشركة أو المحاسب.")
    def _scope(self, company_id):
        """تطبيق نطاق الشركات والفروع المسموح للمستخدم بالوصول إليها."""
        if not can_access_company(self.request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة.")


class TreasuryAccountViewSet(TreasuryBaseViewSet):
    serializer_class = TreasuryAccountSerializer
    queryset = TreasuryAccount.objects.select_related("company", "branch", "account", "fx_position").all()
    filterset_fields = ("company", "branch", "kind", "is_active")
    search_fields = ("code", "name_ar", "bank_name", "account_number")

    def get_queryset(self):
        """تجهيز السجلات المسموح بعرضها بعد تطبيق نطاق الشركة والفرع."""
        qs = self.queryset.filter(company__in=accessible_companies(self.request.user))
        if not self.request.user.is_superuser or is_platform_owner(self.request.user):
            from django.db.models import Q
            branches = accessible_branches(self.request.user)
            company_wide_ids = [
                company_id for company_id in qs.values_list("company_id", flat=True).distinct()
                if has_company_wide_branch_access(self.request.user, company_id)
            ]
            qs = qs.filter(
                Q(branch__in=branches)
                | Q(branch__isnull=True, company_id__in=company_wide_ids)
            )
        return qs

    @transaction.atomic
    def perform_create(self, serializer):
        """حفظ سجل جديد بعد التحقق من الصلاحيات والعلاقات المرتبطة."""
        company = serializer.validated_data["company"]
        company_id = company.pk
        branch = serializer.validated_data.get("branch")
        self._scope(company_id)
        self._write(company_id)
        if branch and not can_access_branch(self.request.user, branch.pk, company_id):
            raise PermissionDenied("لا تملك صلاحية إدارة حساب خزينة في هذا الفرع.")
        account = serializer.save()
        if account.opening_balance > 0:
            from accounting.models import JournalEntry, JournalLine
            from accounting.services import get_account_for_role, post_journal
            from companies.models import Branch, FiscalYear

            posting_branch = account.branch
            if not posting_branch:
                accessible_ids = accessible_branches(self.request.user, company_id).values_list("id", flat=True)
                posting_branch = Branch.objects.filter(
                    company=company, is_active=True, pk__in=accessible_ids
                ).order_by("-is_head_office", "code").first()
            if not posting_branch:
                raise ValidationError("لا يوجد فرع نشط ومتاح لتسجيل القيد الافتتاحي للخزينة.")
            year = FiscalYear.objects.filter(
                company=company, status=FiscalYear.Status.OPEN
            ).order_by("start_date").first()
            if not year:
                raise ValidationError("لا توجد سنة مالية مفتوحة لتسجيل الرصيد الافتتاحي.")
            retained_earnings = get_account_for_role(company, "retained_earnings")
            entry = JournalEntry.objects.create(
                company=company, branch=posting_branch, fiscal_year=year,
                entry_number=f"TR-OPEN-{account.pk}", entry_date=year.start_date,
                description=f"الرصيد الافتتاحي للخزينة {account.name_ar}",
                source_type=JournalEntry.Source.OPENING, created_by=self.request.user,
            )
            JournalLine.objects.bulk_create([
                JournalLine(
                    entry=entry, account=account.account, debit=account.opening_balance,
                    credit=0, description=f"رصيد افتتاحي - {account.name_ar}",
                ),
                JournalLine(
                    entry=entry, account=retained_earnings, debit=0,
                    credit=account.opening_balance, description=f"مقابل الرصيد الافتتاحي - {account.name_ar}",
                ),
            ])
            posted = post_journal(entry.pk, actor=self.request.user)
            account.opening_journal_entry = posted
            account.save(update_fields=("opening_journal_entry",))

    @decorators.action(detail=True, methods=("post",), url_path="revalue")
    def revalue(self, request, pk=None):
        """إعادة تقييم مركز عملة الحساب بسعر صرف معتمد وتسجيل قيد الفروق."""
        account = self.get_object()
        self._scope(account.company_id)
        self._write(account.company_id)
        if account.branch_id and not can_access_branch(request.user, account.branch_id, account.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع حساب الخزينة.")
        fiscal_year_id = request.data.get("fiscal_year")
        revaluation_date = dateparse.parse_date(str(request.data.get("revaluation_date", "")))
        rate_date = dateparse.parse_date(str(request.data.get("rate_date", "")))
        if not fiscal_year_id or not revaluation_date or not rate_date:
            raise ValidationError({
                "detail": "أدخل السنة المالية وتاريخ إعادة التقييم وتاريخ سعر الصرف المعتمد."
            })
        branch = account.branch
        if not branch:
            branch = accessible_branches(request.user, account.company_id).filter(
                is_active=True
            ).order_by("-is_head_office", "code").first()
        if not branch:
            raise ValidationError({"branch": "لا يوجد فرع نشط ومتاح لتسجيل قيد إعادة التقييم."})
        try:
            result = revalue_treasury_fx_account(
                account_id=account.pk, fiscal_year_id=fiscal_year_id,
                revaluation_date=revaluation_date, rate_date=rate_date,
                branch=branch, user=request.user,
            )
        except DjangoValidationError as exc:
            raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages) from exc
        return response.Response({
            "id": result.pk,
            "account": result.account_id,
            "currency": result.account.currency_code,
            "revaluation_date": result.revaluation_date,
            "rate_date": result.rate_date,
            "rate": result.rate,
            "foreign_amount": result.foreign_amount,
            "previous_carrying_amount": result.previous_carrying_amount,
            "new_carrying_amount": result.new_carrying_amount,
            "difference": result.difference,
            "journal_entry": result.journal_entry_id,
            "journal_number": result.journal_entry.entry_number if result.journal_entry_id else "",
            "status": "posted" if result.journal_entry_id else "no_difference",
            "can_reverse": can_reverse_treasury_fx_revaluation(result),
            "reversal_journal_entry": result.reversal_journal_entry_id,
            "reversal_journal_number": "",
            "reversed_at": result.reversed_at,
            "reversed_by_name": "",
        }, status=status.HTTP_201_CREATED)

    @decorators.action(detail=True, methods=("post",), url_path="reverse-revaluation")
    def reverse_revaluation(self, request, pk=None):
        """عكس آخر إعادة تقييم آمنة مع عكس القيد واستعادة القيمة الدفترية لمركز العملة."""
        account = self.get_object()
        self._scope(account.company_id)
        self._write(account.company_id)
        revaluation_id = request.data.get("revaluation_id")
        if not str(revaluation_id or "").isdigit():
            raise ValidationError({"revaluation_id": "حدد سجل إعادة التقييم المطلوب عكسه."})
        revaluation = TreasuryFxRevaluation.objects.select_related(
            "journal_entry",
        ).filter(pk=int(revaluation_id), account=account).first()
        if not revaluation:
            raise ValidationError({"revaluation_id": "سجل إعادة التقييم لا يتبع حساب الخزينة المحدد."})
        branch_id = (
            account.branch_id
            or (revaluation.journal_entry.branch_id if revaluation.journal_entry_id else None)
        )
        if branch_id and not can_access_branch(request.user, branch_id, account.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع إعادة التقييم.")
        entry_date_raw = request.data.get("entry_date")
        entry_date = dateparse.parse_date(str(entry_date_raw)) if entry_date_raw else timezone.localdate()
        if not entry_date:
            raise ValidationError({"entry_date": "صيغة تاريخ العكس يجب أن تكون YYYY-MM-DD."})
        try:
            result = reverse_treasury_fx_revaluation(
                revaluation_id=revaluation.pk, entry_date=entry_date, user=request.user,
            )
        except DjangoValidationError as exc:
            raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages) from exc
        return response.Response({
            "id": result.pk,
            "status": "reversed",
            "revaluation_date": result.revaluation_date,
            "difference": result.difference,
            "journal_number": result.journal_entry.entry_number if result.journal_entry_id else "",
            "reversal_journal_entry": result.reversal_journal_entry_id,
            "reversal_journal_number": (
                result.reversal_journal_entry.entry_number if result.reversal_journal_entry_id else ""
            ),
            "reversed_at": result.reversed_at,
        }, status=status.HTTP_201_CREATED)

    @decorators.action(detail=True, methods=("get",), url_path="revaluations")
    def revaluations(self, request, pk=None):
        """عرض سجل إعادة التقييم وقيود فروق العملة للحساب."""
        account = self.get_object()
        self._scope(account.company_id)
        if account.branch_id and not can_access_branch(request.user, account.branch_id, account.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع حساب الخزينة.")
        rows = TreasuryFxRevaluation.objects.filter(account=account).select_related(
            "journal_entry", "reversal_journal_entry", "reversed_by",
        ).order_by("-revaluation_date", "-id")
        if not account.branch_id:
            # Shared accounts have no branch metadata on zero-difference records;
            # hide those records rather than exposing another branch's revaluation history.
            rows = rows.filter(
                journal_entry__branch__in=accessible_branches(request.user, account.company_id)
            )
        return response.Response([{
            "id": row.pk,
            "account": row.account_id,
            "currency": account.currency_code,
            "revaluation_date": row.revaluation_date,
            "rate_date": row.rate_date,
            "rate": row.rate,
            "foreign_amount": row.foreign_amount,
            "previous_carrying_amount": row.previous_carrying_amount,
            "new_carrying_amount": row.new_carrying_amount,
            "difference": row.difference,
            "journal_entry": row.journal_entry_id,
            "journal_number": row.journal_entry.entry_number if row.journal_entry_id else "",
            "status": (
                "reversed" if row.reversal_journal_entry_id
                else "posted" if row.journal_entry_id else "no_difference"
            ),
            "can_reverse": can_reverse_treasury_fx_revaluation(row),
            "reversal_journal_entry": row.reversal_journal_entry_id,
            "reversal_journal_number": (
                row.reversal_journal_entry.entry_number if row.reversal_journal_entry_id else ""
            ),
            "reversed_at": row.reversed_at,
            "reversed_by_name": (
                row.reversed_by.get_full_name() or row.reversed_by.username
                if row.reversed_by_id else ""
            ),
        } for row in rows])

    def perform_update(self, serializer):
        """حفظ التعديلات المسموح بها بعد إعادة فحص نطاق السجل."""
        instance = serializer.instance
        company_id = serializer.validated_data.get("company", instance.company).pk
        if company_id != instance.company_id:
            raise ValidationError("لا يمكن نقل حساب الخزينة بين الشركات بعد إنشائه.")
        branch = serializer.validated_data.get("branch", instance.branch)
        self._scope(company_id)
        self._write(company_id)
        if branch and not can_access_branch(self.request.user, branch.pk, company_id):
            raise PermissionDenied("لا تملك صلاحية نقل حساب الخزينة إلى هذا الفرع.")
        serializer.save()

    def perform_destroy(self, instance):
        """حذف أو تعطيل السجل وفق قيود الارتباط والحالة المالية."""
        self._write(instance.company_id)
        if instance.opening_journal_entry_id:
            raise ValidationError("لا يمكن حذف خزينة لها قيد افتتاحي؛ عطّل الحساب للحفاظ على أثر الرصيد الافتتاحي ومنع تكراره.")
        if TreasuryTransaction.objects.filter(source_account=instance).exists() or TreasuryTransaction.objects.filter(destination_account=instance).exists():
            raise ValidationError("لا يمكن حذف خزينة لها حركات؛ أوقفها بدلًا من حذفها.")
        instance.delete()


class TreasuryTransactionViewSet(TreasuryBaseViewSet):
    serializer_class = TreasuryTransactionSerializer
    queryset = TreasuryTransaction.objects.select_related(
        "company", "branch", "fiscal_year", "source_account", "destination_account",
        "counter_account", "journal_entry"
    ).all()
    filterset_fields = ("company", "branch", "kind", "status", "transaction_date")
    search_fields = ("document_number", "description", "reference")
    ordering_fields = ("transaction_date", "document_number", "amount", "created_at")

    def get_queryset(self):
        """تجهيز السجلات المسموح بعرضها بعد تطبيق نطاق الشركة والفرع."""
        qs = self.queryset.filter(company__in=accessible_companies(self.request.user))
        if not self.request.user.is_superuser or is_platform_owner(self.request.user):
            qs = qs.filter(branch__in=accessible_branches(self.request.user))
        return qs

    @transaction.atomic
    def perform_create(self, serializer):
        """حفظ سجل جديد بعد التحقق من الصلاحيات والعلاقات المرتبطة."""
        data = serializer.validated_data
        self._scope(data["company"].pk); self._write(data["company"].pk)
        if not can_access_branch(self.request.user, data["branch"].pk, data["company"].pk):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع.")
        doc = serializer.save(created_by=self.request.user)
        refresh_financial_approval(
            FinancialApprovalRequest.DocumentType.TREASURY_PAYMENT,
            doc, self.request.user,
        )

    @transaction.atomic
    def perform_update(self, serializer):
        """حفظ التعديلات المسموح بها بعد إعادة فحص نطاق السجل."""
        instance = serializer.instance
        from hr.models import PayrollPosting
        if PayrollPosting.objects.filter(payment_treasury_transaction=instance).exists():
            raise ValidationError("مستند صرف المرتبات مرتبط بدورة مرتبات ولا يمكن تعديله من شاشة الخزينة.")
        if instance.status != TreasuryTransaction.Status.DRAFT:
            raise ValidationError("المستند المرحّل غير قابل للتعديل.")
        company = serializer.validated_data.get("company", instance.company)
        branch = serializer.validated_data.get("branch", instance.branch)
        if company.pk != instance.company_id:
            raise ValidationError("لا يمكن نقل مستند الخزينة بين الشركات.")
        self._scope(company.pk)
        self._write(company.pk)
        if not can_access_branch(self.request.user, branch.pk, company.pk):
            raise PermissionDenied("لا تملك صلاحية نقل مستند الخزينة إلى هذا الفرع.")
        doc = serializer.save()
        refresh_financial_approval(
            FinancialApprovalRequest.DocumentType.TREASURY_PAYMENT,
            doc, self.request.user,
        )

    def perform_destroy(self, instance):
        """حذف أو تعطيل السجل وفق قيود الارتباط والحالة المالية."""
        self._write(instance.company_id)
        from hr.models import PayrollPosting
        if PayrollPosting.objects.filter(payment_treasury_transaction=instance).exists():
            raise ValidationError("مستند صرف المرتبات مرتبط بدورة مرتبات ولا يمكن حذفه من شاشة الخزينة.")
        if not can_access_branch(self.request.user, instance.branch_id, instance.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع المستند.")
        if instance.status == TreasuryTransaction.Status.POSTED:
            raise ValidationError("لا يمكن حذف مستند خزينة مرحّل؛ استخدم إجراء العكس المحاسبي المعتمد.")
        instance.delete()

    @decorators.action(detail=True, methods=("post",))
    def post(self, request, pk=None):
        """ترحيل المستند أو تنفيذ إجراء الترحيل المرتبط به."""
        doc = self.get_object()
        self._write(doc.company_id)
        if not can_access_branch(request.user, doc.branch_id, doc.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع.")
        try:
            doc = post_treasury_transaction(doc.pk, request.user)
        except DjangoValidationError as exc:
            raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages) from exc
        return response.Response(self.get_serializer(doc).data, status=status.HTTP_200_OK)


    @decorators.action(detail=True, methods=("post",), url_path="reverse")
    def reverse(self, request, pk=None):
        """عكس سند خزينة مستقل مع تحديث حالته وربط القيد العكسي."""
        doc = self.get_object()
        self._write(doc.company_id)
        if not can_access_branch(request.user, doc.branch_id, doc.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع.")
        entry_date_raw = request.data.get("entry_date")
        entry_date = dateparse.parse_date(str(entry_date_raw)) if entry_date_raw else timezone.localdate()
        if not entry_date:
            raise ValidationError({"entry_date": "صيغة تاريخ العكس يجب أن تكون YYYY-MM-DD."})
        try:
            doc = reverse_treasury_transaction(transaction_id=doc.pk, entry_date=entry_date, user=request.user)
        except DjangoValidationError as exc:
            raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages) from exc
        return response.Response(self.get_serializer(doc).data, status=status.HTTP_200_OK)

    @decorators.action(detail=False, methods=("get",), url_path="cash-flow")
    def cash_flow(self, request):
        """تجميع حركة النقدية والتدفقات المرتبطة بحسابات الخزينة."""
        company_id = request.query_params.get("company")
        if not company_id or not company_id.isdigit():
            raise ValidationError({"company": "حدد الشركة لعرض التدفقات النقدية."})
        company_id = int(company_id)
        self._scope(company_id)
        rows = self.get_queryset().filter(
            company_id=company_id, status=TreasuryTransaction.Status.POSTED
        ).exclude(kind=TreasuryTransaction.Kind.TRANSFER).select_related(
            "source_account", "journal_entry", "branch"
        )
        branch_id = request.query_params.get("branch")
        if branch_id:
            if not branch_id.isdigit() or not can_access_branch(request.user, int(branch_id), company_id):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")
            rows = rows.filter(branch_id=int(branch_id))
        date_from_raw, date_to_raw = request.query_params.get("date_from"), request.query_params.get("date_to")
        date_from = dateparse.parse_date(date_from_raw) if date_from_raw else None
        date_to = dateparse.parse_date(date_to_raw) if date_to_raw else None
        if date_from_raw and not date_from:
            raise ValidationError({"date_from": "صيغة التاريخ يجب أن تكون YYYY-MM-DD."})
        if date_to_raw and not date_to:
            raise ValidationError({"date_to": "صيغة التاريخ يجب أن تكون YYYY-MM-DD."})
        if date_from and date_to and date_from > date_to:
            raise ValidationError("تاريخ البداية يجب ألا يتجاوز تاريخ النهاية.")
        if date_from:
            rows = rows.filter(transaction_date__gte=date_from)
        if date_to:
            rows = rows.filter(transaction_date__lte=date_to)
        categories = []
        for code, label in TreasuryTransaction.CashFlowCategory.choices:
            selected = list(rows.filter(cash_flow_category=code))
            inflows = sum((row.amount for row in selected if row.kind == TreasuryTransaction.Kind.RECEIPT), start=0)
            outflows = sum((row.amount for row in selected if row.kind == TreasuryTransaction.Kind.PAYMENT), start=0)
            categories.append({"category": code, "label": label, "inflows": inflows,
                               "outflows": outflows, "net": inflows - outflows})
        details = [{
            "id": row.pk, "date": row.transaction_date, "document_number": row.document_number,
            "description": row.description, "branch": row.branch.name_ar,
            "category": row.cash_flow_category, "kind": row.kind, "amount": row.amount,
            "signed_amount": row.amount if row.kind == TreasuryTransaction.Kind.RECEIPT else -row.amount,
            "treasury_account": row.source_account.name_ar, "journal_entry": row.journal_entry_id,
        } for row in rows.order_by("transaction_date", "id")]
        return response.Response({
            "company": company_id, "date_from": date_from, "date_to": date_to,
            "categories": categories, "rows": details,
            "net_cash_flow": sum((item["net"] for item in categories), start=0),
            "note": "يعرض الحركات النقدية المرحلة من الخزائن والبنوك فقط؛ التحويلات الداخلية مستبعدة.",
        })


class BankReconciliationViewSet(TreasuryBaseViewSet):
    serializer_class = BankReconciliationSerializer
    queryset = BankReconciliation.objects.select_related("company", "bank_account").prefetch_related("lines").all()
    filterset_fields = ("company", "bank_account", "status", "statement_date")
    ordering_fields = ("statement_date", "created_at", "statement_number")

    def get_queryset(self):
        """تجهيز السجلات المسموح بعرضها بعد تطبيق نطاق الشركة والفرع."""
        qs = self.queryset.filter(company__in=accessible_companies(self.request.user))
        if not self.request.user.is_superuser or is_platform_owner(self.request.user):
            from django.db.models import Q
            branches = accessible_branches(self.request.user)
            company_wide_ids = [
                company_id for company_id in qs.values_list("company_id", flat=True).distinct()
                if has_company_wide_branch_access(self.request.user, company_id)
            ]
            qs = qs.filter(
                Q(bank_account__branch__in=branches)
                | Q(bank_account__branch__isnull=True, company_id__in=company_wide_ids)
            )
        return qs

    def get_object(self):
        """إخفاء السجلات غير المصرح بها مع توضيح منع الوصول داخل الشركة."""
        try:
            return super().get_object()
        except Http404:
            lookup = self.kwargs.get(self.lookup_url_kwarg or self.lookup_field)
            hidden = self.queryset.filter(
                company__in=accessible_companies(self.request.user),
                **{self.lookup_field: lookup},
            ).first()
            if hidden:
                bank = hidden.bank_account
                if (
                    (bank.branch_id and not can_access_branch(
                        self.request.user, bank.branch_id, hidden.company_id
                    ))
                    or (
                        not bank.branch_id
                        and not has_company_wide_branch_access(
                            self.request.user, hidden.company_id
                        )
                    )
                ):
                    raise PermissionDenied("لا تملك صلاحية الوصول إلى تسوية هذا الحساب البنكي.") from None
            raise

    def perform_create(self, serializer):
        """حفظ سجل جديد بعد التحقق من الصلاحيات والعلاقات المرتبطة."""
        data = serializer.validated_data
        company_id = data["company"].pk
        self._scope(company_id); self._write(company_id)
        bank = data["bank_account"]
        if bank.company_id != company_id or bank.kind != TreasuryAccount.Kind.BANK or not bank.is_active:
            raise ValidationError("اختر حسابًا بنكيًا نشطًا تابعًا للشركة.")
        if bank.branch_id and not can_access_branch(self.request.user, bank.branch_id, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الحساب البنكي.")
        if not bank.branch_id and not has_company_wide_branch_access(self.request.user, company_id):
            raise PermissionDenied("الحساب البنكي العام بلا فرع يتطلب صلاحية الوصول إلى جميع فروع الشركة لإجراء التسوية.")
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
        """حفظ التعديلات المسموح بها بعد إعادة فحص نطاق السجل."""
        instance = serializer.instance
        if instance.status != BankReconciliation.Status.DRAFT:
            raise ValidationError("التسوية المعتمدة غير قابلة للتعديل.")
        company = serializer.validated_data.get("company", instance.company)
        bank = serializer.validated_data.get("bank_account", instance.bank_account)
        if company.pk != instance.company_id:
            raise ValidationError("لا يمكن نقل تسوية البنك بين الشركات.")
        self._write(instance.company_id)
        if bank.branch_id and not can_access_branch(self.request.user, bank.branch_id, instance.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الحساب البنكي.")
        if not bank.branch_id and not has_company_wide_branch_access(self.request.user, instance.company_id):
            raise PermissionDenied("الحساب البنكي العام بلا فرع يتطلب صلاحية الوصول إلى جميع فروع الشركة لإجراء التسوية.")
        serializer.save()

    def perform_destroy(self, instance):
        """حذف أو تعطيل السجل وفق قيود الارتباط والحالة المالية."""
        self._write(instance.company_id)
        if instance.status != BankReconciliation.Status.DRAFT:
            raise ValidationError("لا يمكن حذف تسوية معتمدة.")
        instance.delete()

    @decorators.action(detail=True, methods=("post",))
    def import_csv(self, request, pk=None):
        """استيراد كشف البنك والتحقق من صيغ السطور والتواريخ والمبالغ."""
        recon = self.get_object()
        self._write(recon.company_id)
        if recon.status != BankReconciliation.Status.DRAFT:
            raise ValidationError("لا يمكن استيراد كشف إلى تسوية معتمدة.")
        if recon.bank_account.branch_id and not can_access_branch(request.user, recon.bank_account.branch_id, recon.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الحساب البنكي.")
        upload = request.FILES.get("file")
        if not upload:
            raise ValidationError({"file": "اختر ملف CSV أولًا."})
        if upload.size > 5 * 1024 * 1024:
            raise ValidationError({"file": "الحد الأقصى لحجم الملف 5 ميجابايت."})
        try:
            raw = upload.read().decode("utf-8-sig")
            dialect = csv.Sniffer().sniff(raw[:4096], delimiters=",;\t")
            reader = csv.DictReader(io.StringIO(raw), dialect=dialect)
        except (UnicodeDecodeError, csv.Error):
            raise ValidationError({"file": "تعذر قراءة الملف. احفظه بصيغة CSV UTF-8."}) from None
        if not reader.fieldnames:
            raise ValidationError({"file": "الملف فارغ أو لا يحتوي على عناوين أعمدة."})
        headers = {str(h).strip().lower(): h for h in reader.fieldnames if h}
        date_key = next((headers[k] for k in ("date", "transaction_date", "التاريخ") if k in headers), None)
        amount_key = next((headers[k] for k in ("amount", "value", "المبلغ", "القيمة") if k in headers), None)
        desc_key = next((headers[k] for k in ("description", "details", "البيان", "الوصف") if k in headers), None)
        ref_key = next((headers[k] for k in ("reference", "ref", "المرجع") if k in headers), None)
        if not date_key or not amount_key:
            raise ValidationError({"file": "يجب أن يحتوي الملف على عمودي date و amount على الأقل؛ ويمكن إضافة description و reference."})
        parsed = []
        errors = []
        for row_no, row in enumerate(reader, start=2):
            if not any(str(v or "").strip() for v in row.values()):
                continue
            if len(parsed) >= 5000:
                raise ValidationError({"file": "الحد الأقصى 5000 حركة في الملف الواحد."})
            try:
                raw_date = str(row.get(date_key, "")).strip()
                try:
                    movement_date = date.fromisoformat(raw_date)
                except ValueError:
                    movement_date = datetime.strptime(raw_date, "%d/%m/%Y").date()
                amount = Decimal(str(row.get(amount_key, "")).strip().replace(",", ""))
                if amount == 0 or not amount.is_finite():
                    raise ValueError("المبلغ يجب ألا يساوي صفرًا.")
                if movement_date > recon.statement_date:
                    raise ValueError("تاريخ الحركة بعد تاريخ كشف البنك.")
                parsed.append({
                    "reconciliation": recon,
                    "line_number": len(parsed) + 1,
                    "transaction_date": movement_date,
                    "amount": amount,
                    "description": str(row.get(desc_key, "") if desc_key else "").strip()[:500],
                    "reference": str(row.get(ref_key, "") if ref_key else "").strip()[:120],
                })
            except (ValueError, InvalidOperation, TypeError):
                errors.append(f"السطر {row_no}: تاريخ أو مبلغ غير صحيح.")
        if errors:
            raise ValidationError({"file": errors[:20]})
        if not parsed:
            raise ValidationError({"file": "لم يتم العثور على حركات صالحة للاستيراد."})
        with transaction.atomic():
            locked = BankReconciliation.objects.select_for_update().get(pk=recon.pk)
            if locked.status != BankReconciliation.Status.DRAFT or locked.lines.exists():
                raise ValidationError("الاستيراد متاح لتسوية مسودة فارغة فقط؛ لا يمكن الكتابة فوق حركات موجودة.")
            BankStatementLine.objects.bulk_create([BankStatementLine(**item) for item in parsed])
        return response.Response({"imported": len(parsed), "message": f"تم استيراد {len(parsed)} حركة دون تعديل أي حركات سابقة."}, status=status.HTTP_201_CREATED)

    @decorators.action(detail=True, methods=("post",))
    def add_line(self, request, pk=None):
        """إضافة سطر كشف بنكي إلى جلسة التسوية الحالية."""
        recon = self.get_object()
        self._write(recon.company_id)
        if recon.status != BankReconciliation.Status.DRAFT:
            raise ValidationError("لا يمكن إضافة سطور إلى تسوية معتمدة.")
        payload = request.data.copy()
        payload["reconciliation"] = recon.pk
        serializer = BankStatementLineSerializer(data=payload)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return response.Response(serializer.data, status=status.HTTP_201_CREATED)

    @transaction.atomic
    @decorators.action(detail=True, methods=("post",), url_path="record-adjustment")
    def record_adjustment(self, request, pk=None):
        """إنشاء مسودة سند خزينة مقابل رسم أو فائدة أو حركة بنكية غير مسجلة."""
        recon = self.get_object()
        self._write(recon.company_id)
        if recon.status != BankReconciliation.Status.DRAFT:
            raise ValidationError("لا يمكن إنشاء تسوية محاسبية من كشف معتمد.")
        if recon.bank_account.currency_code != recon.company.base_currency.code:
            raise ValidationError("إنشاء سند التسوية المباشر متاح حاليًا للحسابات بالعملة الأساسية فقط؛ لا تُنشئ قيدًا أجنبيًا دون سعر صرف معتمد.")
        if recon.bank_account.branch_id:
            branch_id = recon.bank_account.branch_id
        else:
            branch_id = request.data.get("branch")
        try:
            line = BankStatementLine.objects.select_for_update().get(
                pk=request.data.get("line_id"), reconciliation=recon
            )
        except (BankStatementLine.DoesNotExist, ValueError, TypeError):
            raise ValidationError("سطر كشف البنك غير موجود.") from None
        if line.matched_transaction_id:
            raise ValidationError("سطر كشف البنك تمت مطابقته بالفعل.")
        if line.adjustment_transaction_id:
            raise ValidationError("تم إنشاء سند تسوية لهذا السطر بالفعل.")
        try:
            fiscal_year_id = int(request.data.get("fiscal_year"))
            counter_account_id = int(request.data.get("counter_account"))
            branch_id = int(branch_id)
        except (TypeError, ValueError):
            raise ValidationError("حدد السنة المالية والحساب المقابل والفرع.") from None
        from companies.models import Branch, FiscalYear
        from accounting.models import ChartOfAccount
        branch = Branch.objects.filter(pk=branch_id, company_id=recon.company_id).first()
        fiscal_year = FiscalYear.objects.filter(pk=fiscal_year_id, company_id=recon.company_id).first()
        counter_account = ChartOfAccount.objects.filter(pk=counter_account_id, company_id=recon.company_id).first()
        if not branch or not can_access_branch(request.user, branch.pk, recon.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع سند التسوية.")
        if not fiscal_year or fiscal_year.status != FiscalYear.Status.OPEN or not (
            fiscal_year.start_date <= line.transaction_date <= fiscal_year.end_date
        ):
            raise ValidationError("اختر سنة مالية مفتوحة تشمل تاريخ حركة كشف البنك.")
        if not counter_account or counter_account.is_group or not counter_account.allow_posting or not counter_account.is_active:
            raise ValidationError("اختر حسابًا مقابلًا تفصيليًا نشطًا وقابلًا للترحيل.")
        from .serializers import TreasuryTransactionSerializer
        kind = (
            TreasuryTransaction.Kind.RECEIPT
            if line.amount > 0 else TreasuryTransaction.Kind.PAYMENT
        )
        payload = {
            "company": recon.company_id,
            "branch": branch.pk,
            "fiscal_year": fiscal_year.pk,
            "source_account": recon.bank_account_id,
            "counter_account": counter_account.pk,
            "kind": kind,
            "document_number": f"BANK-ADJ-{recon.pk}-{line.pk}",
            "transaction_date": line.transaction_date.isoformat(),
            "amount": str(abs(line.amount)),
            "description": (line.description or "تسوية حركة كشف بنك")[:500],
            "reference": line.reference,
            "cash_flow_category": request.data.get(
                "cash_flow_category", TreasuryTransaction.CashFlowCategory.OPERATING
            ),
        }
        serializer = TreasuryTransactionSerializer(data=payload)
        serializer.is_valid(raise_exception=True)
        doc = serializer.save(created_by=request.user)
        refresh_financial_approval(
            FinancialApprovalRequest.DocumentType.TREASURY_PAYMENT,
            doc, request.user,
        )
        line.adjustment_transaction = doc
        line.save(update_fields=("adjustment_transaction",))
        return response.Response({
            "line": BankStatementLineSerializer(line).data,
            "transaction": TreasuryTransactionSerializer(doc).data,
            "message": "تم إنشاء مسودة سند مقابل لحركة كشف البنك. رحّل السند وفق صلاحيات الاعتماد ثم ارجع لمطابقته.",
        }, status=status.HTTP_201_CREATED)

    @transaction.atomic
    @decorators.action(detail=True, methods=("post",))
    def match(self, request, pk=None):
        """مطابقة سطر كشف البنك مع الحركة المالية المقابلة."""
        recon = self.get_object()
        self._write(recon.company_id)
        if not recon.bank_account.branch_id and not has_company_wide_branch_access(
            request.user, recon.company_id
        ):
            raise PermissionDenied("مطابقة كشف حساب بنكي عام تتطلب صلاحية الوصول إلى جميع فروع الشركة.")
        if recon.status != BankReconciliation.Status.DRAFT:
            raise ValidationError("التسوية ليست مسودة.")
        try:
            line = BankStatementLine.objects.select_for_update().get(pk=request.data.get("line_id"), reconciliation=recon)
            doc = TreasuryTransaction.objects.select_for_update().get(pk=request.data.get("transaction_id"), company_id=recon.company_id, status=TreasuryTransaction.Status.POSTED, transaction_date__lte=recon.statement_date)
        except (BankStatementLine.DoesNotExist, TreasuryTransaction.DoesNotExist, ValueError, TypeError):
            raise ValidationError("سطر كشف البنك أو الحركة المحاسبية غير موجودة.") from None
        if not can_access_branch(request.user, doc.branch_id, doc.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الحركة المحاسبية.")
        if recon.bank_account.branch_id and doc.branch_id != recon.bank_account.branch_id:
            raise ValidationError("الحركة لا تخص فرع الحساب البنكي محل التسوية.")
        signed_amount = _signed_bank_transaction_amount(doc, recon.bank_account)
        if line.amount != signed_amount:
            raise ValidationError("قيمة السطر واتجاهه لا يطابقان حركة الدفاتر.")
        if BankStatementLine.objects.filter(matched_transaction=doc).exclude(reconciliation=recon).exists():
            raise ValidationError("هذه الحركة مرتبطة بالفعل بتسوية أخرى؛ لا يمكن مطابقتها مرتين.")
        if TreasuryTransaction.objects.filter(bank_statement_matches__isnull=False, bank_statement_matches__reconciliation__status=BankReconciliation.Status.RECONCILED, pk=doc.pk).exists():
            raise ValidationError("هذه الحركة سبق تسويتها في كشف بنكي معتمد.")
        if BankStatementLine.objects.filter(reconciliation=recon, matched_transaction=doc).exclude(pk=line.pk).exists():
            raise ValidationError("الحركة مرتبطة بالفعل بسطر آخر في نفس التسوية.")
        line.matched_transaction = doc
        line.matched_at = timezone.now()
        line.save(update_fields=("matched_transaction", "matched_at"))
        return response.Response(BankStatementLineSerializer(line).data)

    @decorators.action(detail=True, methods=("post",))
    def unmatch(self, request, pk=None):
        """إلغاء المطابقة مع الإبقاء على بيانات سطر الكشف."""
        recon = self.get_object()
        self._write(recon.company_id)
        if recon.status != BankReconciliation.Status.DRAFT:
            raise ValidationError("لا يمكن فك المطابقة بعد اعتماد التسوية.")
        try:
            line = BankStatementLine.objects.get(pk=request.data.get("line_id"), reconciliation=recon)
        except (BankStatementLine.DoesNotExist, ValueError, TypeError):
            raise ValidationError("سطر كشف البنك غير موجود.") from None
        line.matched_transaction = None
        line.matched_at = None
        line.save(update_fields=("matched_transaction", "matched_at"))
        return response.Response(BankStatementLineSerializer(line).data)

    @decorators.action(detail=True, methods=("get",))
    def summary(self, request, pk=None):
        """إعداد ملخص جلسة التسوية والفروق بين الدفاتر وكشف البنك."""
        recon = self.get_object()
        lines = recon.lines.select_related("matched_transaction")
        matched_ids = list(lines.filter(matched_transaction__isnull=False).values_list("matched_transaction_id", flat=True))
        branch_ids = accessible_branches(request.user, recon.company_id).values_list("id", flat=True)
        if recon.bank_account.branch_id:
            branch_ids = [recon.bank_account.branch_id]
        book_balance = _bank_book_balance(recon.bank_account, recon.statement_date, branch_ids)
        statement_balance = recon.opening_balance + sum((line.amount for line in lines), Decimal("0"))
        outstanding = TreasuryTransaction.objects.filter(company_id=recon.company_id, branch_id__in=branch_ids, status=TreasuryTransaction.Status.POSTED, transaction_date__lte=recon.statement_date).exclude(pk__in=matched_ids).exclude(bank_statement_matches__reconciliation__status=BankReconciliation.Status.RECONCILED).filter(
            models.Q(source_account=recon.bank_account) | models.Q(destination_account=recon.bank_account)
        ).order_by("transaction_date", "id")
        outstanding_rows = []
        outstanding_total = Decimal("0")
        for doc in outstanding:
            signed = _signed_bank_transaction_amount(doc, recon.bank_account)
            outstanding_total += signed
            outstanding_rows.append({"id": doc.id, "date": doc.transaction_date, "document_number": doc.document_number, "description": doc.description, "amount": str(signed)})
        expected_book = recon.closing_balance + outstanding_total
        return response.Response({
            "reconciliation": self.get_serializer(recon).data,
            "statement_calculated": str(statement_balance),
            "statement_difference": str(recon.closing_balance - statement_balance),
            "book_balance": str(book_balance),
            "outstanding_total": str(outstanding_total),
            "expected_book_balance": str(expected_book),
            "unexplained_difference": str(book_balance - expected_book),
            "matched_lines": lines.filter(matched_transaction__isnull=False).count(),
            "unmatched_lines": lines.filter(matched_transaction__isnull=True).count(),
            "lines": BankStatementLineSerializer(lines, many=True).data,
            "outstanding_transactions": outstanding_rows,
        })

    @decorators.action(detail=True, methods=("post",))
    def reconcile(self, request, pk=None):
        """اعتماد نتيجة التسوية بعد التحقق من اكتمال المطابقات."""
        with transaction.atomic():
            scoped = self.get_object()
            recon = BankReconciliation.objects.select_for_update().get(pk=scoped.pk)
            self._write(recon.company_id)
            if recon.bank_account.branch_id and not can_access_branch(request.user, recon.bank_account.branch_id, recon.company_id):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الحساب البنكي.")
            if recon.status != BankReconciliation.Status.DRAFT:
                raise ValidationError("التسوية معتمدة بالفعل.")
            if recon.lines.filter(matched_transaction__isnull=True).exists():
                raise ValidationError("لا يمكن الاعتماد قبل مطابقة جميع سطور كشف البنك.")
            line_total = recon.opening_balance + (recon.lines.aggregate(total=Sum("amount"))["total"] or Decimal("0"))
            if line_total != recon.closing_balance:
                raise ValidationError("رصيد الإقفال لا يساوي الرصيد الافتتاحي مضافًا إليه صافي حركات الكشف.")
            matched_ids = list(recon.lines.values_list("matched_transaction_id", flat=True))
            branch_ids = accessible_branches(request.user, recon.company_id).values_list("id", flat=True)
            if recon.bank_account.branch_id:
                branch_ids = [recon.bank_account.branch_id]
            book_balance = _bank_book_balance(recon.bank_account, recon.statement_date, branch_ids)
            outstanding = TreasuryTransaction.objects.filter(company_id=recon.company_id, branch_id__in=branch_ids, status=TreasuryTransaction.Status.POSTED, transaction_date__lte=recon.statement_date).exclude(pk__in=matched_ids).exclude(bank_statement_matches__reconciliation__status=BankReconciliation.Status.RECONCILED).filter(
                models.Q(source_account=recon.bank_account) | models.Q(destination_account=recon.bank_account)
            )
            outstanding_total = Decimal("0")
            for doc in outstanding:
                outstanding_total += _signed_bank_transaction_amount(doc, recon.bank_account)
            unexplained = book_balance - (recon.closing_balance + outstanding_total)
            if unexplained != 0:
                raise ValidationError({"difference": f"لا يمكن اعتماد التسوية؛ يوجد فرق غير مفسر في الدفاتر بقيمة {unexplained}."})
            recon.status = BankReconciliation.Status.RECONCILED
            recon.reconciled_at = timezone.now()
            recon.save(update_fields=("status", "reconciled_at"))
        return response.Response(self.get_serializer(recon).data)


class BankStatementLineViewSet(TreasuryBaseViewSet):
    serializer_class = BankStatementLineSerializer
    queryset = BankStatementLine.objects.select_related("reconciliation", "matched_transaction").all()

    def get_queryset(self):
        """تطبيق نطاق الفروع نفسه المستخدم في تسويات الحسابات البنكية."""
        qs = self.queryset.filter(reconciliation__company__in=accessible_companies(self.request.user))
        if not self.request.user.is_superuser or is_platform_owner(self.request.user):
            branches = accessible_branches(self.request.user)
            company_wide_ids = [
                company_id for company_id in qs.values_list("reconciliation__company_id", flat=True).distinct()
                if has_company_wide_branch_access(self.request.user, company_id)
            ]
            qs = qs.filter(
                models.Q(reconciliation__bank_account__branch__in=branches)
                | models.Q(
                    reconciliation__bank_account__branch__isnull=True,
                    reconciliation__company_id__in=company_wide_ids,
                )
            )
        return qs

    def _assert_reconciliation_branch_access(self, recon):
        """رفض الوصول إلى حسابات الشركة العامة دون صلاحية شاملة لكل فروعها."""
        branch_id = recon.bank_account.branch_id
        if branch_id:
            allowed = can_access_branch(self.request.user, branch_id, recon.company_id)
        else:
            allowed = has_company_wide_branch_access(self.request.user, recon.company_id)
        if not allowed:
            raise PermissionDenied("لا تملك صلاحية الوصول إلى نطاق فروع هذا الحساب البنكي.")

    def perform_create(self, serializer):
        """حفظ سجل جديد بعد التحقق من الصلاحيات والعلاقات المرتبطة."""
        recon = serializer.validated_data["reconciliation"]
        self._write(recon.company_id)
        self._assert_reconciliation_branch_access(recon)
        if recon.status != BankReconciliation.Status.DRAFT:
            raise ValidationError("التسوية ليست مسودة.")
        serializer.save()

    def perform_update(self, serializer):
        """حفظ التعديلات المسموح بها بعد إعادة فحص نطاق السجل."""
        recon = serializer.instance.reconciliation
        self._write(recon.company_id)
        self._assert_reconciliation_branch_access(recon)
        if recon.status != BankReconciliation.Status.DRAFT or serializer.instance.matched_transaction_id:
            raise ValidationError("لا يمكن تعديل سطر مطابق أو ضمن تسوية معتمدة.")
        if serializer.validated_data.get("reconciliation", recon).pk != recon.pk:
            raise ValidationError("لا يمكن نقل سطر كشف بين تسويتين.")
        serializer.save()

    def perform_destroy(self, instance):
        """حذف أو تعطيل السجل وفق قيود الارتباط والحالة المالية."""
        recon = instance.reconciliation
        self._write(recon.company_id)
        self._assert_reconciliation_branch_access(recon)
        if recon.status != BankReconciliation.Status.DRAFT or instance.matched_transaction_id:
            raise ValidationError("لا يمكن حذف سطر مطابق أو ضمن تسوية معتمدة.")
        instance.delete()
