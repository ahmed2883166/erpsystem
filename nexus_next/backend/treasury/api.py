import csv
import io
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import decorators, permissions, response, status, viewsets
from django.db import models
from rest_framework.exceptions import PermissionDenied, ValidationError

from companies.access import accessible_branches, accessible_companies, can_access_branch, can_access_company, has_company_role
from .models import TreasuryAccount, TreasuryTransaction, BankReconciliation, BankStatementLine
from .serializers import TreasuryAccountSerializer, TreasuryTransactionSerializer, BankReconciliationSerializer, BankStatementLineSerializer
from .services import post_treasury_transaction


class TreasuryBaseViewSet(viewsets.ModelViewSet):
    permission_classes = (permissions.IsAuthenticated,)
    def _write(self, company_id):
        if not has_company_role(self.request.user, company_id, ("admin", "accountant")):
            raise PermissionDenied("تحتاج إلى دور مدير الشركة أو المحاسب.")
    def _scope(self, company_id):
        if not can_access_company(self.request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة.")


class TreasuryAccountViewSet(TreasuryBaseViewSet):
    serializer_class = TreasuryAccountSerializer
    queryset = TreasuryAccount.objects.select_related("company", "branch", "account").all()
    filterset_fields = ("company", "branch", "kind", "is_active")
    search_fields = ("code", "name_ar", "bank_name", "account_number")

    def get_queryset(self):
        qs = self.queryset.filter(company__in=accessible_companies(self.request.user))
        if not self.request.user.is_superuser:
            from django.db.models import Q
            qs = qs.filter(Q(branch__isnull=True) | Q(branch__in=accessible_branches(self.request.user)))
        return qs

    def perform_create(self, serializer):
        company_id = serializer.validated_data["company"].pk
        self._scope(company_id); self._write(company_id); serializer.save()

    def perform_update(self, serializer):
        company_id = serializer.validated_data.get("company", serializer.instance.company).pk
        self._scope(company_id); self._write(company_id); serializer.save()

    def perform_destroy(self, instance):
        self._write(instance.company_id)
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
        qs = self.queryset.filter(company__in=accessible_companies(self.request.user))
        if not self.request.user.is_superuser:
            qs = qs.filter(branch__in=accessible_branches(self.request.user))
        return qs

    def perform_create(self, serializer):
        data = serializer.validated_data
        self._scope(data["company"].pk); self._write(data["company"].pk)
        if not can_access_branch(self.request.user, data["branch"].pk, data["company"].pk):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع.")
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
        instance = serializer.instance
        if instance.status != TreasuryTransaction.Status.DRAFT:
            raise ValidationError("المستند المرحّل غير قابل للتعديل.")
        company = serializer.validated_data.get("company", instance.company)
        self._scope(company.pk); self._write(company.pk); serializer.save()

    def perform_destroy(self, instance):
        self._write(instance.company_id); instance.delete()

    @decorators.action(detail=True, methods=("post",))
    def post(self, request, pk=None):
        doc = self.get_object()
        self._write(doc.company_id)
        if not can_access_branch(request.user, doc.branch_id, doc.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع.")
        try:
            doc = post_treasury_transaction(doc.pk, request.user)
        except DjangoValidationError as exc:
            raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages) from exc
        return response.Response(self.get_serializer(doc).data, status=status.HTTP_200_OK)


class BankReconciliationViewSet(TreasuryBaseViewSet):
    serializer_class = BankReconciliationSerializer
    queryset = BankReconciliation.objects.select_related("company", "bank_account").prefetch_related("lines").all()
    filterset_fields = ("company", "bank_account", "status", "statement_date")
    ordering_fields = ("statement_date", "created_at", "statement_number")

    def get_queryset(self):
        qs = self.queryset.filter(company__in=accessible_companies(self.request.user))
        if not self.request.user.is_superuser:
            from django.db.models import Q
            qs = qs.filter(Q(bank_account__branch__isnull=True) | Q(bank_account__branch__in=accessible_branches(self.request.user)))
        return qs

    def perform_create(self, serializer):
        data = serializer.validated_data
        company_id = data["company"].pk
        self._scope(company_id); self._write(company_id)
        bank = data["bank_account"]
        if bank.company_id != company_id or bank.kind != TreasuryAccount.Kind.BANK or not bank.is_active:
            raise ValidationError("اختر حسابًا بنكيًا نشطًا تابعًا للشركة.")
        if bank.branch_id and not can_access_branch(self.request.user, bank.branch_id, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الحساب البنكي.")
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
        if serializer.instance.status != BankReconciliation.Status.DRAFT:
            raise ValidationError("التسوية المعتمدة غير قابلة للتعديل.")
        self._write(serializer.instance.company_id)
        serializer.save()

    def perform_destroy(self, instance):
        self._write(instance.company_id)
        if instance.status != BankReconciliation.Status.DRAFT:
            raise ValidationError("لا يمكن حذف تسوية معتمدة.")
        instance.delete()

    @decorators.action(detail=True, methods=("post",))
    def import_csv(self, request, pk=None):
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
            raise ValidationError({"file": "تعذر قراءة الملف. احفظه بصيغة CSV UTF-8."})
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
    @decorators.action(detail=True, methods=("post",))
    def match(self, request, pk=None):
        recon = self.get_object()
        self._write(recon.company_id)
        if recon.status != BankReconciliation.Status.DRAFT:
            raise ValidationError("التسوية ليست مسودة.")
        try:
            line = BankStatementLine.objects.select_for_update().get(pk=request.data.get("line_id"), reconciliation=recon)
            doc = TreasuryTransaction.objects.select_for_update().get(pk=request.data.get("transaction_id"), company_id=recon.company_id, status=TreasuryTransaction.Status.POSTED, transaction_date__lte=recon.statement_date)
        except (BankStatementLine.DoesNotExist, TreasuryTransaction.DoesNotExist, ValueError, TypeError):
            raise ValidationError("سطر كشف البنك أو الحركة المحاسبية غير موجودة.")
        bank_id = recon.bank_account_id
        if doc.source_account_id == bank_id:
            signed_amount = doc.amount if doc.kind == TreasuryTransaction.Kind.RECEIPT else -doc.amount
        elif doc.destination_account_id == bank_id and doc.kind == TreasuryTransaction.Kind.TRANSFER:
            signed_amount = doc.amount
        else:
            raise ValidationError("الحركة لا تخص الحساب البنكي محل التسوية.")
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
        recon = self.get_object()
        self._write(recon.company_id)
        if recon.status != BankReconciliation.Status.DRAFT:
            raise ValidationError("لا يمكن فك المطابقة بعد اعتماد التسوية.")
        try:
            line = BankStatementLine.objects.get(pk=request.data.get("line_id"), reconciliation=recon)
        except (BankStatementLine.DoesNotExist, ValueError, TypeError):
            raise ValidationError("سطر كشف البنك غير موجود.")
        line.matched_transaction = None
        line.matched_at = None
        line.save(update_fields=("matched_transaction", "matched_at"))
        return response.Response(BankStatementLineSerializer(line).data)

    @decorators.action(detail=True, methods=("get",))
    def summary(self, request, pk=None):
        recon = self.get_object()
        lines = recon.lines.select_related("matched_transaction")
        matched_ids = list(lines.filter(matched_transaction__isnull=False).values_list("matched_transaction_id", flat=True))
        book_lines = recon.bank_account.account.journal_lines.filter(entry__status="posted", entry__entry_date__lte=recon.statement_date)
        totals = book_lines.aggregate(debit=Sum("debit"), credit=Sum("credit"))
        book_balance = (totals["debit"] or Decimal("0")) - (totals["credit"] or Decimal("0"))
        statement_balance = recon.opening_balance + sum((line.amount for line in lines), Decimal("0"))
        outstanding = TreasuryTransaction.objects.filter(company_id=recon.company_id, status=TreasuryTransaction.Status.POSTED, transaction_date__lte=recon.statement_date).exclude(pk__in=matched_ids).exclude(bank_statement_matches__reconciliation__status=BankReconciliation.Status.RECONCILED).filter(
            models.Q(source_account=recon.bank_account) | models.Q(destination_account=recon.bank_account)
        ).order_by("transaction_date", "id")
        outstanding_rows = []
        outstanding_total = Decimal("0")
        for doc in outstanding:
            signed = doc.amount if ((doc.source_account_id == recon.bank_account_id and doc.kind == TreasuryTransaction.Kind.RECEIPT) or (doc.destination_account_id == recon.bank_account_id)) else -doc.amount
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
            bank_lines = recon.bank_account.account.journal_lines.filter(entry__status="posted", entry__entry_date__lte=recon.statement_date)
            totals = bank_lines.aggregate(debit=Sum("debit"), credit=Sum("credit"))
            book_balance = (totals["debit"] or Decimal("0")) - (totals["credit"] or Decimal("0"))
            outstanding = TreasuryTransaction.objects.filter(company_id=recon.company_id, status=TreasuryTransaction.Status.POSTED, transaction_date__lte=recon.statement_date).exclude(pk__in=matched_ids).exclude(bank_statement_matches__reconciliation__status=BankReconciliation.Status.RECONCILED).filter(
                models.Q(source_account=recon.bank_account) | models.Q(destination_account=recon.bank_account)
            )
            outstanding_total = Decimal("0")
            for doc in outstanding:
                outstanding_total += doc.amount if ((doc.source_account_id == recon.bank_account_id and doc.kind == TreasuryTransaction.Kind.RECEIPT) or doc.destination_account_id == recon.bank_account_id) else -doc.amount
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
        qs = self.queryset.filter(reconciliation__company__in=accessible_companies(self.request.user))
        if not self.request.user.is_superuser:
            qs = qs.filter(models.Q(reconciliation__bank_account__branch__isnull=True) | models.Q(reconciliation__bank_account__branch__in=accessible_branches(self.request.user)))
        return qs

    def perform_create(self, serializer):
        recon = serializer.validated_data["reconciliation"]
        self._write(recon.company_id)
        if recon.status != BankReconciliation.Status.DRAFT:
            raise ValidationError("التسوية ليست مسودة.")
        if recon.bank_account.branch_id and not can_access_branch(self.request.user, recon.bank_account.branch_id, recon.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الحساب البنكي.")
        serializer.save()

    def perform_update(self, serializer):
        recon = serializer.instance.reconciliation
        self._write(recon.company_id)
        if recon.status != BankReconciliation.Status.DRAFT or serializer.instance.matched_transaction_id:
            raise ValidationError("لا يمكن تعديل سطر مطابق أو ضمن تسوية معتمدة.")
        if serializer.validated_data.get("reconciliation", recon).pk != recon.pk:
            raise ValidationError("لا يمكن نقل سطر كشف بين تسويتين.")
        serializer.save()

    def perform_destroy(self, instance):
        self._write(instance.reconciliation.company_id)
        if instance.reconciliation.status != BankReconciliation.Status.DRAFT or instance.matched_transaction_id:
            raise ValidationError("لا يمكن حذف سطر مطابق أو ضمن تسوية معتمدة.")
        instance.delete()
