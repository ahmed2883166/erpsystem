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
from .models import TreasuryAccount, TreasuryTransaction, TreasuryCheque, TreasuryFxPosition, TreasuryFxRevaluation, BankReconciliation, BankStatementLine, BankStatementMatchAllocation, CashCommitment, CashCommitmentPayment, CashCommitmentSchedule, CashCommitmentOccurrence
from .serializers import TreasuryAccountSerializer, TreasuryTransactionSerializer, TreasuryChequeSerializer, BankReconciliationSerializer, BankStatementLineSerializer, CashCommitmentSerializer, CashCommitmentPaymentSerializer, CashCommitmentScheduleSerializer
from .services import (
    post_treasury_transaction, reverse_treasury_transaction, revalue_treasury_fx_account,
    reverse_treasury_fx_revaluation, can_reverse_treasury_fx_revaluation,
)
from approvals.models import FinancialApprovalRequest
from approvals.services import refresh_financial_approval
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException
import re
import zipfile


def _parse_statement_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y%m%d", "%y%m%d"):
        try:
            return datetime.strptime(raw[:10], fmt).date()
        except ValueError:
            continue
    raise ValueError("تاريخ الحركة غير صحيح.")


def _parse_statement_amount(value):
    raw = str(value or "").strip().replace(" ", "")
    if "," in raw and "." not in raw and raw.count(",") == 1 and len(raw.rsplit(",", 1)[1]) == 2:
        raw = raw.replace(",", ".")
    else:
        raw = raw.replace(",", "")
    if raw.upper().endswith("CR"):
        raw = raw[:-2]
    if raw.upper().endswith("DR"):
        raw = "-" + raw[:-2]
    amount = Decimal(raw)
    if not amount.is_finite() or amount == 0:
        raise ValueError("المبلغ يجب أن يكون رقمًا غير صفري.")
    if amount != amount.quantize(Decimal("0.0001")) or abs(amount) >= Decimal("10000000000000000"):
        raise ValueError("المبلغ يجب ألا يتجاوز 16 رقمًا صحيحًا وأربع منازل عشرية.")
    return amount


def _statement_row(movement_date, amount, description="", reference=""):
    return {
        "transaction_date": movement_date,
        "amount": amount,
        "description": str(description or "").strip()[:500],
        "reference": str(reference or "").strip()[:120],
    }


def _statement_cell(row, index):
    return row[index] if index is not None and index < len(row) else ""


def _ofx_tag(block, name):
    match = re.search(rf"<{name}>([^<\r\n]+)", block, flags=re.I)
    return match.group(1).strip() if match else ""


def _parse_bank_statement(upload, statement_date):
    """قراءة CSV/XLSX/OFX/MT940 إلى صفوف موحدة دون حفظها."""
    filename = (upload.name or "").lower()
    extension = filename.rsplit(".", 1)[-1] if "." in filename else ""
    raw = upload.read()
    rows = []
    if extension in ("csv", "txt"):
        text = raw.decode("utf-8-sig")
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        if not reader.fieldnames:
            raise ValueError("الملف لا يحتوي على عناوين أعمدة.")
        headers = {str(h).strip().lower(): h for h in reader.fieldnames if h}
        date_key = next((headers[k] for k in ("date", "transaction_date", "التاريخ") if k in headers), None)
        amount_key = next((headers[k] for k in ("amount", "value", "المبلغ", "القيمة") if k in headers), None)
        desc_key = next((headers[k] for k in ("description", "details", "البيان", "الوصف") if k in headers), None)
        ref_key = next((headers[k] for k in ("reference", "ref", "fitid", "المرجع") if k in headers), None)
        if not date_key or not amount_key:
            raise ValueError("يجب وجود عمودي date و amount على الأقل.")
        for row in reader:
            if not any(str(v or "").strip() for v in row.values()):
                continue
            rows.append(_statement_row(
                _parse_statement_date(row.get(date_key)),
                _parse_statement_amount(row.get(amount_key)),
                row.get(desc_key) if desc_key else "",
                row.get(ref_key) if ref_key else "",
            ))
    elif extension == "xlsx":
        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        sheet = workbook.active
        values = sheet.iter_rows(values_only=True)
        header_row = next(values, None)
        if not header_row:
            raise ValueError("ملف Excel فارغ.")
        headers = {str(v).strip().lower(): i for i, v in enumerate(header_row) if v is not None}
        date_index = next((headers[k] for k in ("date", "transaction_date", "التاريخ") if k in headers), None)
        amount_index = next((headers[k] for k in ("amount", "value", "المبلغ", "القيمة") if k in headers), None)
        desc_index = next((headers[k] for k in ("description", "details", "البيان", "الوصف") if k in headers), None)
        ref_index = next((headers[k] for k in ("reference", "ref", "المرجع") if k in headers), None)
        if date_index is None or amount_index is None:
            raise ValueError("يجب وجود عمودي date و amount على الأقل في Excel.")
        for row in values:
            if not row or not any(v is not None and str(v).strip() for v in row):
                continue
            rows.append(_statement_row(
                _parse_statement_date(_statement_cell(row, date_index)),
                _parse_statement_amount(_statement_cell(row, amount_index)),
                _statement_cell(row, desc_index), _statement_cell(row, ref_index),
            ))
        workbook.close()
    elif extension in ("ofx", "qfx"):
        text = raw.decode("utf-8-sig", errors="replace")
        for block in re.findall(r"<STMTTRN>(.*?)</STMTTRN>", text, flags=re.I | re.S):
            posted = re.match(r"\d{8}", _ofx_tag(block, "DTPOSTED"))
            rows.append(_statement_row(
                _parse_statement_date(posted.group(0) if posted else _ofx_tag(block, "DTPOSTED")),
                _parse_statement_amount(_ofx_tag(block, "TRNAMT")),
                _ofx_tag(block, "MEMO") or _ofx_tag(block, "NAME"), _ofx_tag(block, "FITID"),
            ))
        if not rows:
            raise ValueError("لم يتم العثور على حركات STMTTRN في ملف OFX.")
    elif extension in ("mt940", "sta", "940"):
        text = raw.decode("utf-8-sig", errors="replace")
        for block in re.split(r"(?=^:61:)", text, flags=re.M):
            match = re.search(r"^:61:(\d{6})(?:\d{4})?([CD])([A-Z])?([0-9,]+(?:\.[0-9]+)?)", block, flags=re.M)
            if not match:
                continue
            amount = _parse_statement_amount(match.group(4).replace(",", "."))
            amount = -abs(amount) if match.group(2) == "D" else abs(amount)
            ref_match = re.search(r"^:61:.*?//([^\r\n]+)", block, flags=re.M)
            desc_match = re.search(r"^:86:(.*(?:\n[^:][^\n]*)*)", block, flags=re.M)
            rows.append(_statement_row(
                _parse_statement_date(match.group(1)), amount,
                desc_match.group(1).replace("\n", " ").strip() if desc_match else "",
                ref_match.group(1).strip() if ref_match else "",
            ))
        if not rows:
            raise ValueError("لم يتم العثور على حركات :61: في ملف MT940.")
    else:
        raise ValueError("الصيغ المدعومة: CSV وXLSX وOFX/QFX وMT940.")
    if not rows:
        raise ValueError("لم يتم العثور على حركات صالحة داخل الملف.")
    if len(rows) > 5000:
        raise ValueError("الحد الأقصى 5000 حركة في الملف الواحد.")
    for index, row in enumerate(rows, start=1):
        if row["transaction_date"] > statement_date:
            raise ValueError(f"تاريخ الحركة في السطر {index} بعد تاريخ كشف البنك.")
    return rows


def _statement_duplicate_key(row):
    reference = (row.get("reference") or "").strip().casefold()
    description = (row.get("description") or "").strip().casefold()
    return (row["transaction_date"], Decimal(row["amount"]).quantize(Decimal("0.0001")), reference or description)


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



def _allocated_line_amount(line):
    return line.allocations.aggregate(total=Sum("amount"))["total"] or Decimal("0")


def _allocated_transaction_amount(doc, bank_account):
    return doc.reconciliation_allocations.filter(
        line__reconciliation__bank_account=bank_account,
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0")


def _allocate_bank_match(recon, line_id, transaction_id, raw_amount, user):
    """تخصيص مبلغ جزئي بين سطر كشف البنك وحركة دفترية مع قفل الطرفين."""
    if not recon.bank_account.branch_id and not has_company_wide_branch_access(
        user, recon.company_id
    ):
        raise PermissionDenied("مطابقة كشف حساب بنكي عام تتطلب صلاحية الوصول إلى جميع فروع الشركة.")
    if recon.status != BankReconciliation.Status.DRAFT:
        raise ValidationError("التسوية ليست مسودة.")
    try:
        line = BankStatementLine.objects.select_for_update().get(
            pk=line_id, reconciliation=recon,
        )
        doc = TreasuryTransaction.objects.select_for_update().get(
            pk=transaction_id, company_id=recon.company_id,
            status=TreasuryTransaction.Status.POSTED,
            transaction_date__lte=recon.statement_date,
        )
    except (BankStatementLine.DoesNotExist, TreasuryTransaction.DoesNotExist, ValueError, TypeError):
        raise ValidationError("سطر كشف البنك أو الحركة المحاسبية غير موجودة.") from None
    if not can_access_branch(user, doc.branch_id, doc.company_id):
        raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الحركة المحاسبية.")
    if recon.bank_account.branch_id and doc.branch_id != recon.bank_account.branch_id:
        raise ValidationError("الحركة لا تخص فرع الحساب البنكي محل التسوية.")
    signed_amount = _signed_bank_transaction_amount(doc, recon.bank_account)
    if (line.amount > 0) != (signed_amount > 0):
        raise ValidationError("اتجاه حركة كشف البنك لا يطابق اتجاه الحركة الدفترية.")
    try:
        amount = Decimal(str(raw_amount))
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError({"amount": "أدخل مبلغ المطابقة بصيغة رقمية صحيحة."}) from None
    if not amount.is_finite() or amount <= 0:
        raise ValidationError({"amount": "مبلغ المطابقة يجب أن يكون أكبر من صفر."})
    if amount != amount.quantize(Decimal("0.0001")):
        raise ValidationError({"amount": "مبلغ المطابقة لا يقبل أكثر من أربع منازل عشرية."})

    line_remaining = abs(line.amount) - _allocated_line_amount(line)
    if amount > line_remaining:
        raise ValidationError({"amount": f"مبلغ المطابقة يتجاوز المتبقي في سطر البنك ({line_remaining})."})

    other_reconciliation_allocations = doc.reconciliation_allocations.filter(
        line__reconciliation__bank_account=recon.bank_account,
    ).exclude(line__reconciliation=recon)
    if other_reconciliation_allocations.exists():
        raise ValidationError("الحركة مرتبطة بالفعل بتسوية بنكية أخرى لهذا الحساب.")
    transaction_allocated = _allocated_transaction_amount(doc, recon.bank_account)
    transaction_remaining = abs(signed_amount) - transaction_allocated
    if amount > transaction_remaining:
        raise ValidationError({"amount": f"مبلغ المطابقة يتجاوز المتبقي من الحركة الدفترية ({transaction_remaining})."})

    allocation, created = BankStatementMatchAllocation.objects.get_or_create(
        line=line, transaction=doc, defaults={"amount": amount},
    )
    if not created:
        allocation.amount += amount
        allocation.save(update_fields=("amount",))
    line_total = _allocated_line_amount(line)
    doc_total = _allocated_transaction_amount(doc, recon.bank_account)
    line.matched_transaction = None
    if (line_total == abs(line.amount) and line.allocations.count() == 1 and doc_total == abs(signed_amount) and doc.reconciliation_allocations.filter(line__reconciliation__bank_account=recon.bank_account).count() == 1):
        line.matched_transaction = doc
    line.matched_at = timezone.now()
    line.save(update_fields=("matched_transaction", "matched_at"))
    return line


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
            from companies.models import Branch, FiscalYear, Currency
            foreign_opening = account.currency_code != company.base_currency.code
            opening_currency = Currency.objects.filter(code=account.currency_code, is_active=True).first() if foreign_opening else None
            if foreign_opening and not opening_currency:
                raise ValidationError("عملة الرصيد الافتتاحي غير نشطة أو غير موجودة.")
            fx_snapshot = ({
                "transaction_currency": opening_currency,
                "transaction_amount": account.opening_foreign_amount,
                "exchange_rate": account.opening_exchange_rate,
                "exchange_rate_date": account.opening_exchange_rate_date,
            } if foreign_opening else {})

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
            if foreign_opening and account.opening_exchange_rate_date > year.start_date:
                raise ValidationError({
                    "opening_exchange_rate_date": "تاريخ سعر الصرف يجب ألا يتجاوز تاريخ القيد الافتتاحي."
                })
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
                    **fx_snapshot,
                ),
                JournalLine(
                    entry=entry, account=retained_earnings, debit=0,
                    credit=account.opening_balance, description=f"مقابل الرصيد الافتتاحي - {account.name_ar}",
                ),
            ])
            posted = post_journal(entry.pk, actor=self.request.user)
            account.opening_journal_entry = posted
            account.save(update_fields=("opening_journal_entry",))
            if foreign_opening:
                from .models import TreasuryFxPosition
                TreasuryFxPosition.objects.create(
                    account=account,
                    foreign_amount=account.opening_foreign_amount,
                    carrying_base_amount=account.opening_balance,
                )

    @decorators.action(detail=False, methods=("get",), url_path="liquidity-summary")
    def liquidity_summary(self, request):
        """الرصيد الدفتري الحالي لحسابات الخزينة مع فصل الأرصدة الاسمية الأجنبية."""
        from accounting.models import JournalLine
        from django.db.models import Sum

        company_id = request.query_params.get("company")
        if not company_id or not company_id.isdigit():
            raise ValidationError({"company": "حدد الشركة لعرض مركز السيولة."})
        company_id = int(company_id)
        self._scope(company_id)

        as_of_raw = request.query_params.get("as_of")
        as_of = dateparse.parse_date(as_of_raw) if as_of_raw else timezone.localdate()
        if not as_of:
            raise ValidationError({"as_of": "صيغة التاريخ يجب أن تكون YYYY-MM-DD."})
        if as_of != timezone.localdate():
            raise ValidationError({"as_of": "مركز السيولة الحالي يعرض تاريخ اليوم فقط؛ لا يعرض أرصدة تاريخية للعملات الأجنبية."})

        branch_id = request.query_params.get("branch")
        if branch_id:
            if not branch_id.isdigit() or not can_access_branch(request.user, int(branch_id), company_id):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")
            branch_id = int(branch_id)

        visible_accounts = self.get_queryset().filter(company_id=company_id, is_active=True).select_related(
            "company__base_currency", "branch", "account", "fx_position",
        )
        if branch_id:
            visible_accounts = visible_accounts.filter(models.Q(branch_id=branch_id) | models.Q(branch__isnull=True))

        accessible_branch_ids = list(
            accessible_branches(request.user, company_id).values_list("id", flat=True)
        )
        if branch_id:
            scoped_branch_ids = [branch_id]
        else:
            scoped_branch_ids = accessible_branch_ids

        rows = []
        total_base = Decimal("0")
        for account in visible_accounts.order_by("kind", "code"):
            account_branch_ids = (
                [account.branch_id] if account.branch_id else scoped_branch_ids
            )
            account_branch_ids = [value for value in account_branch_ids if value in scoped_branch_ids]
            if not account_branch_ids:
                continue
            totals = JournalLine.objects.filter(
                account_id=account.account_id,
                entry__company_id=company_id,
                entry__branch_id__in=account_branch_ids,
                entry__entry_date__lte=as_of,
                entry__status__in=("posted", "reversed"),
            ).exclude(
                entry__source_type="closing",
            ).aggregate(debit=Sum("debit"), credit=Sum("credit"))
            base_balance = (
                (totals["debit"] or Decimal("0"))
                - (totals["credit"] or Decimal("0"))
            )
            # Legacy accounts without an opening journal retain their stored opening value.
            if not account.opening_journal_entry_id:
                base_balance += account.opening_balance

            is_foreign = account.currency_code != account.company.base_currency.code
            try:
                position = account.fx_position
            except TreasuryFxPosition.DoesNotExist:
                position = None
            foreign_amount = position.foreign_amount if position else (
                account.opening_foreign_amount if is_foreign else Decimal("0")
            )
            if is_foreign and position:
                base_balance = position.carrying_base_amount
            total_base += base_balance
            rows.append({
                "id": account.pk,
                "code": account.code,
                "name": account.name_ar,
                "kind": account.kind,
                "branch_id": account.branch_id,
                "branch_name": account.branch.name_ar if account.branch_id else "كل الفروع",
                "currency": account.currency_code,
                "balance": str(foreign_amount if is_foreign else base_balance),
                "foreign_amount": str(foreign_amount) if is_foreign else None,
                "base_balance": str(base_balance),
                "is_foreign": is_foreign,
            })

        return response.Response({
            "company": company_id,
            "as_of": as_of,
            "base_currency": visible_accounts.first().company.base_currency.code if rows else "",
            "branch": branch_id,
            "scope": "branch" if branch_id else "accessible_branches",
            "total_base_balance": str(total_base),
            "accounts": rows,
            "note": "الأرصدة محسوبة حتى اليوم من القيود المرحلة فقط، مع استبعاد قيود الإقفال. التحويلات الداخلية لا تغيّر إجمالي الشركة، والعملات الأجنبية معروضة اسميًا وبقيمتها الدفترية الأساسية.",
        })


    @decorators.action(detail=False, methods=("get",), url_path="liquidity-forecast")
    def liquidity_forecast(self, request):
        """توقع السيولة من الأرصدة المفتوحة ودورات المرتبات المعتمدة دون تكرار المقبوضات أو المدفوعات."""
        from collections import defaultdict
        from datetime import timedelta
        from django.db.models import Sum
        from sales.models import (
            SalesInvoice, CustomerReceipt, CustomerReceiptBatch, CustomerReceiptBatchAllocation, SalesReturn,
        )
        from purchasing.models import PurchaseInvoice, SupplierPayment, SupplierReturn
        from assets.models import AssetPurchaseInvoice, AssetSupplierPayment
        from hr.models import PayrollCycle
        from treasury.models import TreasuryTransaction
        from companies.models import Branch, Company

        company_raw = request.query_params.get("company", "")
        if not company_raw.isdigit():
            raise ValidationError({"company": "حدد الشركة لعرض توقع السيولة."})
        company_id = int(company_raw)
        self._scope(company_id)
        today = timezone.localdate()
        horizon_raw = request.query_params.get("days", "90")
        try:
            horizon_days = int(horizon_raw)
        except (TypeError, ValueError):
            raise ValidationError({"days": "اختر أفقًا صحيحًا: 30 أو 60 أو 90 أو 180 يومًا."}) from None
        if horizon_days not in (30, 60, 90, 180):
            raise ValidationError({"days": "أفق التوقع المتاح 30 أو 60 أو 90 أو 180 يومًا."})
        end_date = today + timedelta(days=horizon_days)
        branch_raw = request.query_params.get("branch")
        branch_id = None
        if branch_raw:
            if not branch_raw.isdigit() or not can_access_branch(request.user, int(branch_raw), company_id):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")
            branch_id = int(branch_raw)
        branch_ids = list(accessible_branches(request.user, company_id).values_list("id", flat=True))
        if branch_id:
            branch_ids = [branch_id]
        if not branch_ids:
            raise PermissionDenied("لا توجد فروع متاحة لعرض توقع السيولة.")
        company = Company.objects.select_related("base_currency").filter(pk=company_id).first()
        if not company:
            raise ValidationError({"company": "الشركة المحددة غير موجودة."})
        base_currency = company.base_currency.code

        daily = defaultdict(lambda: {
            "collections": Decimal("0"), "supplier_payments": Decimal("0"),
            "payroll": Decimal("0"),
        })
        items = []
        coverage_notes = [
            "المبالغ معروضة بالعملة الأساسية للشركة؛ يتم استخدام القيمة الدفترية الأساسية للمستندات الأجنبية.",
            "المستندات المتأخرة تُعرض كتدفق مستحق اليوم مع الاحتفاظ بتاريخ استحقاقها الأصلي.",
            "الشيكات غير المربوطة بفاتورة لا تُضاف منفصلة حتى لا تتكرر مع ذمم العملاء؛ سندات الصرف المرحلة داخلة بالفعل في الرصيد الفعلي.",
            "الالتزامات القديمة المسجلة في قاعدة SQLite المنفصلة غير مضافة إلى توقعات قاعدة AdmRail الحالية.",
        ]

        def add_item(kind, document, party, due_date, amount, label):
            amount = max(Decimal(amount), Decimal("0"))
            if amount <= 0 or due_date > end_date:
                return
            forecast_date = max(due_date, today)
            daily[forecast_date][kind] += amount
            items.append({
                "kind": kind, "document": str(document), "party": str(party),
                "due_date": due_date.isoformat(), "forecast_date": forecast_date.isoformat(),
                "balance": str(amount), "status": label,
                "date_basis": "متأخر" if due_date < today else "تاريخ الاستحقاق",
            })

        # Receivables: posted invoices less posted receipts, posted batch allocations,
        # and posted sales returns. Reversed and draft documents do not reduce the balance.
        sales_invoices = SalesInvoice.objects.filter(
            company_id=company_id, status=SalesInvoice.Status.POSTED,
            invoice_date__lte=end_date,
        ).filter(
            models.Q(branch_id__in=branch_ids)
            | models.Q(delivery__warehouse__branch_id__in=branch_ids)
        ).select_related("customer", "branch").prefetch_related(
            "receipts", "returns",
        )
        batch_paid_by_invoice = {
            row["invoice_id"]: row["total"]
            for row in CustomerReceiptBatchAllocation.objects.filter(
                invoice__in=sales_invoices,
                batch__status=CustomerReceiptBatch.Status.POSTED,
                batch__receipt_date__lte=end_date,
            ).values("invoice_id").annotate(total=Sum("amount"))
        }
        for invoice in sales_invoices:
            posted_receipts = [
                receipt for receipt in invoice.receipts.all()
                if receipt.status == CustomerReceipt.Status.POSTED
                and receipt.receipt_date <= end_date
            ]
            paid = sum((
                receipt.carrying_base_amount if receipt.carrying_base_amount is not None else receipt.amount
                for receipt in posted_receipts
            ), Decimal("0"))
            for receipt in posted_receipts:
                if receipt.receipt_date > today:
                    add_item(
                        "collections", receipt.receipt_number, invoice.customer.name_ar,
                        receipt.receipt_date,
                        receipt.carrying_base_amount if receipt.carrying_base_amount is not None else receipt.amount,
                        "سند قبض مرحّل بتاريخ مستقبلي",
                    )
            batch_paid = batch_paid_by_invoice.get(invoice.pk, Decimal("0"))
            returned = sum((
                item.grand_total for item in invoice.returns.all()
                if item.status == SalesReturn.Status.POSTED and item.return_date <= end_date
            ), Decimal("0"))
            balance = max(invoice.grand_total - paid - batch_paid - returned, Decimal("0"))
            due_date = invoice.invoice_date + timedelta(days=invoice.customer.payment_terms_days)
            add_item(
                "collections", invoice.invoice_number, invoice.customer.name_ar, due_date,
                balance, "رصيد عميل مفتوح",
            )

        # Future-dated batch receipts are counted once at batch level, split by allocations.
        future_batches = CustomerReceiptBatch.objects.filter(
            company_id=company_id, status=CustomerReceiptBatch.Status.POSTED,
            receipt_date__gt=today, receipt_date__lte=end_date, branch_id__in=branch_ids,
        ).prefetch_related("allocations__invoice__customer")
        for batch in future_batches:
            allocated = Decimal("0")
            for allocation in batch.allocations.all():
                allocated += allocation.amount
                add_item(
                    "collections", batch.receipt_number,
                    allocation.invoice.customer.name_ar, batch.receipt_date,
                    allocation.amount, "سند قبض مجمع مرحّل بتاريخ مستقبلي",
                )
            unallocated = max(batch.amount - allocated, Decimal("0"))
            if unallocated:
                add_item(
                    "collections", batch.receipt_number, batch.customer.name_ar,
                    batch.receipt_date, unallocated, "دفعة مقدمة من عميل بتاريخ مستقبلي",
                )

        # Supplier operating invoices: subtract posted payments and returns only.
        purchase_invoices = PurchaseInvoice.objects.filter(
            company_id=company_id, status=PurchaseInvoice.Status.POSTED,
            invoice_date__lte=end_date,
        ).filter(
            models.Q(branch_id__in=branch_ids)
            | models.Q(receipt__warehouse__branch_id__in=branch_ids)
        ).select_related("supplier").prefetch_related("payments", "returns")
        for invoice in purchase_invoices:
            paid = sum((
                payment.carrying_base_amount if payment.carrying_base_amount is not None else payment.amount
                for payment in invoice.payments.all()
                if payment.status == SupplierPayment.Status.POSTED and payment.payment_date <= end_date
            ), Decimal("0"))
            for payment in invoice.payments.all():
                if payment.status == SupplierPayment.Status.POSTED and today < payment.payment_date <= end_date:
                    add_item(
                        "supplier_payments", payment.payment_number, invoice.supplier.name_ar,
                        payment.payment_date,
                        payment.carrying_base_amount if payment.carrying_base_amount is not None else payment.amount,
                        "سند صرف مورد مرحّل بتاريخ مستقبلي",
                    )
            returned = sum((
                item.grand_total for item in invoice.returns.all()
                if item.status == SupplierReturn.Status.POSTED and item.return_date <= end_date
            ), Decimal("0"))
            balance = max(invoice.grand_total - paid - returned, Decimal("0"))
            due_date = invoice.invoice_date + timedelta(days=invoice.supplier.payment_terms_days)
            add_item(
                "supplier_payments", invoice.invoice_number, invoice.supplier.name_ar, due_date,
                balance, "رصيد مورد مفتوح",
            )

        # Fixed-asset supplier invoices are a separate payable source and are
        # included once, independently of purchasing invoices.
        asset_invoices = AssetPurchaseInvoice.objects.filter(
            company_id=company_id, status=AssetPurchaseInvoice.Status.POSTED,
            invoice_date__lte=end_date, branch_id__in=branch_ids,
        ).select_related("supplier").prefetch_related("payments")
        for invoice in asset_invoices:
            paid = sum((
                payment.amount for payment in invoice.payments.all()
                if payment.status == AssetSupplierPayment.Status.POSTED and payment.payment_date <= end_date
            ), Decimal("0"))
            for payment in invoice.payments.all():
                if payment.status == AssetSupplierPayment.Status.POSTED and today < payment.payment_date <= end_date:
                    add_item(
                        "supplier_payments", payment.payment_number, invoice.supplier.name_ar,
                        payment.payment_date, payment.amount,
                        "سند سداد أصل ثابت مرحّل بتاريخ مستقبلي",
                    )
            balance = max(invoice.grand_total - paid, Decimal("0"))
            due_date = invoice.invoice_date + timedelta(days=invoice.supplier.payment_terms_days)
            add_item(
                "supplier_payments", invoice.invoice_number, invoice.supplier.name_ar, due_date,
                balance, "فاتورة أصل ثابت مفتوحة",
            )

        # Approved/posted payroll cycles: count branch net pay, but exclude any
        # branch payment whose treasury transaction has already been posted.
        payroll_cycles = PayrollCycle.objects.filter(
            company_id=company_id,
            status__in=(
                PayrollCycle.Status.APPROVED, PayrollCycle.Status.POSTED,
                PayrollCycle.Status.PAYMENT_PENDING, PayrollCycle.Status.PAID,
            ),
            payment_date__lte=end_date,
        ).prefetch_related("postings__payment_treasury_transaction")
        base_currency_id = company.base_currency_id
        skipped_foreign_payroll = False
        for cycle in payroll_cycles:
            results = cycle.results.filter(
                employee__branch_id__in=branch_ids,
                employee__currency_id=base_currency_id,
            ).values("employee__branch_id").annotate(total=Sum("net_pay"))
            postings = {posting.branch_id: posting for posting in cycle.postings.all()}
            for result in results:
                posting = postings.get(result["employee__branch_id"])
                payment_doc = posting.payment_treasury_transaction if posting else None
                branch = Branch.objects.filter(pk=result["employee__branch_id"]).only("name_ar").first()
                branch_name = branch.name_ar if branch else "مرتبات الفرع"
                payroll_amount = result["total"] or Decimal("0")
                if payment_doc and payment_doc.status == TreasuryTransaction.Status.POSTED:
                    if payment_doc.transaction_date > today:
                        add_item(
                            "payroll", payment_doc.document_number, branch_name,
                            payment_doc.transaction_date, payroll_amount,
                            "سند صرف مرتبات مرحّل بتاريخ مستقبلي",
                        )
                    continue
                add_item(
                    "payroll", f"PAY-{cycle.pk}", branch_name,
                    cycle.payment_date, payroll_amount,
                    "استحقاق مرتبات غير مصروف",
                )
            if cycle.results.filter(employee__branch_id__in=branch_ids).exclude(
                employee__currency_id=base_currency_id
            ).exists():
                skipped_foreign_payroll = True
        if skipped_foreign_payroll:
            coverage_notes.append(
                "بعض نتائج المرتبات بعملة غير العملة الأساسية لم تدخل التوقع؛ لا يوجد لها تحويل عملة معتمد محفوظ في نتيجة المرتب."
            )

        # Commitments outside operational subledgers are forecast-only. Their
        # linked posted payments are already reflected in actual cash and are
        # therefore subtracted from the outstanding commitment balance.
        from treasury.models import CashCommitment, CashCommitmentPayment
        commitment_qs = CashCommitment.objects.filter(
            company_id=company_id, status__in=(CashCommitment.Status.OPEN, CashCommitment.Status.PARTIALLY_PAID),
            due_date__lte=end_date,
        ).select_related("branch")
        if branch_id:
            commitment_qs = commitment_qs.filter(models.Q(branch_id=branch_id) | models.Q(branch__isnull=True))
        elif branch_ids:
            commitment_qs = commitment_qs.filter(models.Q(branch_id__in=branch_ids) | models.Q(branch__isnull=True))
        for commitment in commitment_qs:
            payments = commitment.payments.select_related("reversal_journal_entry").filter(payment_date__lte=today)
            paid_base = sum((
                payment.base_amount for payment in payments
                if not payment.reversal_journal_entry_id
                or payment.reversal_journal_entry.status == JournalEntry.Status.REVERSED
            ), Decimal("0"))
            remaining = max(Decimal("0"), commitment.base_amount - paid_base)
            if remaining <= 0:
                continue
            forecast_date = max(commitment.due_date, today)
            daily[forecast_date].setdefault("other_commitments", Decimal("0"))
            daily[forecast_date]["other_commitments"] += remaining
            items.append({
                "kind": "other_commitments", "document": commitment.title,
                "party": "التزامات نقدية أخرى", "due_date": commitment.due_date.isoformat(),
                "forecast_date": forecast_date.isoformat(), "balance": str(remaining),
                "status": "متأخر" if commitment.due_date < today else "مستحق/متوقع",
                "date_basis": "متأخر" if commitment.due_date < today else "تاريخ الاستحقاق",
            })
        actual_response = self.liquidity_summary(request)
        actual_data = actual_response.data
        actual_cash = Decimal(actual_data["total_base_balance"])
        dates = sorted(daily)
        running = actual_cash
        daily_rows = []
        total_collections = Decimal("0")
        total_supplier_payments = Decimal("0")
        total_payroll = Decimal("0")
        for day in dates:
            row = daily[day]
            collections = row["collections"]
            supplier_payments = row["supplier_payments"]
            payroll = row["payroll"]
            payments = supplier_payments + payroll
            net = collections - payments
            running += net
            total_collections += collections
            total_supplier_payments += supplier_payments
            total_payroll += payroll
            daily_rows.append({
                "date": day.isoformat(), "collections": str(collections),
                "supplier_payments": str(supplier_payments), "payroll": str(payroll),
                "payments": str(payments), "net": str(net),
                "projected_balance": str(running),
            })
        total_payments = total_supplier_payments + total_payroll + total_other_commitments
        return response.Response({
            "company": company_id, "as_of": today.isoformat(), "horizon_days": horizon_days,
            "end_date": end_date.isoformat(), "base_currency": base_currency,
            "branch": branch_id, "actual_cash_balance": str(actual_cash),
            "expected_collections": str(total_collections),
            "expected_supplier_payments": str(total_supplier_payments),
            "expected_payroll": str(total_payroll),
            "expected_payments": str(total_payments),
            "expected_net": str(total_collections - total_payments),
            "projected_closing_balance": str(actual_cash + total_collections - total_payments),
            "daily": daily_rows,
            "items": sorted(items, key=lambda item: (item["forecast_date"], item["kind"], item["document"])),
            "coverage_notes": coverage_notes,
            "note": "التوقع تقديري من الفواتير المرحلة المفتوحة ودورات المرتبات المعتمدة أو المرحلة. الرصيد الفعلي منفصل، ولا تُضاف سندات القبض أو الصرف المرحلة مرة ثانية.",
        })

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

    def _preview_statement_upload(self, request, recon):
        self._write(recon.company_id)
        if recon.status != BankReconciliation.Status.DRAFT:
            raise ValidationError("لا يمكن استيراد كشف إلى تسوية معتمدة.")
        if recon.bank_account.branch_id and not can_access_branch(request.user, recon.bank_account.branch_id, recon.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الحساب البنكي.")
        if not recon.bank_account.branch_id and not has_company_wide_branch_access(request.user, recon.company_id):
            raise PermissionDenied("الحساب البنكي العام يتطلب صلاحية الوصول إلى جميع فروع الشركة.")
        upload = request.FILES.get("file")
        if not upload:
            raise ValidationError({"file": "اختر ملف كشف البنك أولًا."})
        if upload.size > 10 * 1024 * 1024:
            raise ValidationError({"file": "الحد الأقصى لحجم الملف 10 ميجابايت."})
        try:
            parsed = _parse_bank_statement(upload, recon.statement_date)
        except (UnicodeDecodeError, csv.Error, ValueError, InvalidOperation, zipfile.BadZipFile, InvalidFileException) as exc:
            raise ValidationError({"file": str(exc) or "تعذر قراءة ملف كشف البنك."}) from None
        existing = BankStatementLine.objects.filter(
            reconciliation__bank_account_id=recon.bank_account_id,
        ).values("transaction_date", "amount", "reference", "description")
        known = {_statement_duplicate_key(row) for row in existing}
        duplicates, fresh = [], []
        for row in parsed:
            key = _statement_duplicate_key(row)
            if key in known:
                duplicates.append(row)
            else:
                fresh.append(row)
                known.add(key)
        return parsed, fresh, duplicates

    @decorators.action(detail=True, methods=("post",), url_path="preview-import")
    def preview_import(self, request, pk=None):
        """معاينة ملف كشف البنك وحصر الحركات المكررة قبل الحفظ."""
        recon = self.get_object()
        parsed, fresh, duplicates = self._preview_statement_upload(request, recon)
        return response.Response({
            "file_name": request.FILES["file"].name,
            "total": len(parsed), "new_count": len(fresh), "duplicate_count": len(duplicates),
            "duplicates": [{
                "date": row["transaction_date"], "amount": str(row["amount"]),
                "description": row["description"], "reference": row["reference"],
            } for row in duplicates[:100]],
            "rows": [{
                "date": row["transaction_date"], "amount": str(row["amount"]),
                "description": row["description"], "reference": row["reference"],
            } for row in fresh[:100]],
            "rows_truncated": len(fresh) > 100,
        })

    @transaction.atomic
    @decorators.action(detail=True, methods=("post",), url_path="confirm-import")
    def confirm_import(self, request, pk=None):
        """حفظ الحركات الجديدة فقط بعد إعادة فحص التكرار تحت قفل الحساب."""
        recon = self.get_object()
        TreasuryAccount.objects.select_for_update().get(pk=recon.bank_account_id)
        recon.refresh_from_db()
        _, fresh, duplicates = self._preview_statement_upload(request, recon)
        if not fresh:
            return response.Response({
                "imported": 0, "duplicate_count": len(duplicates),
                "message": f"لم تُضف حركات جديدة؛ تم تجاهل {len(duplicates)} حركة مكررة.",
            })
        next_number = (recon.lines.order_by("-line_number").values_list("line_number", flat=True).first() or 0) + 1
        BankStatementLine.objects.bulk_create([
            BankStatementLine(
                reconciliation=recon, line_number=next_number + index,
                transaction_date=row["transaction_date"], amount=row["amount"],
                description=row["description"], reference=row["reference"],
            ) for index, row in enumerate(fresh)
        ])
        return response.Response({
            "imported": len(fresh), "duplicate_count": len(duplicates),
            "message": f"تم استيراد {len(fresh)} حركة جديدة وتجاهل {len(duplicates)} حركة مكررة.",
        }, status=status.HTTP_201_CREATED)

    @decorators.action(detail=True, methods=("post",))
    def import_csv(self, request, pk=None):
        """مسار توافق قديم لاستيراد CSV؛ تستخدم الواجهة الجديدة المعاينة والتأكيد."""
        recon = self.get_object()
        _, fresh, duplicates = self._preview_statement_upload(request, recon)
        if recon.lines.exists():
            raise ValidationError("استخدم معاينة الاستيراد ثم تأكيده لإضافة الحركات دون استبدال السطور الحالية.")
        if not fresh:
            raise ValidationError("كل الحركات في الملف مكررة بالفعل.")
        BankStatementLine.objects.bulk_create([
            BankStatementLine(
                reconciliation=recon, line_number=index,
                transaction_date=row["transaction_date"], amount=row["amount"],
                description=row["description"], reference=row["reference"],
            ) for index, row in enumerate(fresh, start=1)
        ])
        return response.Response({
            "imported": len(fresh),
            "message": f"تم استيراد {len(fresh)} حركة وتجاهل {len(duplicates)} مكررة.",
        }, status=status.HTTP_201_CREATED)

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
        if not recon.bank_account.branch_id and not has_company_wide_branch_access(
            request.user, recon.company_id
        ):
            raise PermissionDenied("إنشاء سند من كشف حساب بنكي عام يتطلب صلاحية الوصول إلى جميع فروع الشركة.")
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
        """مطابقة كاملة أو جزئية لسطر كشف مع حركة دفترية."""
        recon = self.get_object()
        self._write(recon.company_id)
        line_id = request.data.get("line_id")
        transaction_id = request.data.get("transaction_id")
        raw_amount = request.data.get("amount")
        if raw_amount in (None, ""):
            try:
                target_line = BankStatementLine.objects.get(pk=line_id, reconciliation=recon)
            except (BankStatementLine.DoesNotExist, ValueError, TypeError):
                raise ValidationError("سطر كشف البنك غير موجود.") from None
            raw_amount = abs(target_line.amount)
        line = _allocate_bank_match(
            recon, line_id, transaction_id, raw_amount, request.user,
        )
        return response.Response(BankStatementLineSerializer(line).data)

    @transaction.atomic
    @decorators.action(detail=True, methods=("post",))
    def unmatch(self, request, pk=None):
        """فك جميع تخصيصات سطر كشف البنك مع الإبقاء على بياناته."""
        recon = self.get_object()
        self._write(recon.company_id)
        if recon.status != BankReconciliation.Status.DRAFT:
            raise ValidationError("لا يمكن فك المطابقة بعد اعتماد التسوية.")
        try:
            line = BankStatementLine.objects.select_for_update().get(
                pk=request.data.get("line_id"), reconciliation=recon,
            )
        except (BankStatementLine.DoesNotExist, ValueError, TypeError):
            raise ValidationError("سطر كشف البنك غير موجود.") from None
        line.allocations.all().delete()
        line.matched_transaction = None
        line.matched_at = None
        line.save(update_fields=("matched_transaction", "matched_at"))
        return response.Response(BankStatementLineSerializer(line).data)

    @decorators.action(detail=True, methods=("get",))
    def summary(self, request, pk=None):
        """ملخص المطابقة الجزئية والحركات المعلقة وفروق التوقيت."""
        recon = self.get_object()
        lines = recon.lines.select_related("matched_transaction").prefetch_related(
            "allocations__transaction",
        )
        branch_ids = accessible_branches(request.user, recon.company_id).values_list("id", flat=True)
        if recon.bank_account.branch_id:
            branch_ids = [recon.bank_account.branch_id]
        book_balance = _bank_book_balance(recon.bank_account, recon.statement_date, branch_ids)
        statement_balance = recon.opening_balance + sum((line.amount for line in lines), Decimal("0"))
        outstanding = TreasuryTransaction.objects.filter(
            company_id=recon.company_id, branch_id__in=branch_ids,
            status=TreasuryTransaction.Status.POSTED,
            transaction_date__lte=recon.statement_date,
        ).filter(
            models.Q(source_account=recon.bank_account)
            | models.Q(destination_account=recon.bank_account)
        ).order_by("transaction_date", "id")
        outstanding_rows = []
        outstanding_total = Decimal("0")
        for doc in outstanding:
            signed = _signed_bank_transaction_amount(doc, recon.bank_account)
            other_recon = doc.reconciliation_allocations.filter(
                line__reconciliation__bank_account=recon.bank_account,
            ).exclude(line__reconciliation=recon).exists()
            if other_recon:
                continue
            allocated = _allocated_transaction_amount(doc, recon.bank_account)
            remaining = abs(signed) - allocated
            if remaining <= 0:
                continue
            signed_remaining = remaining if signed > 0 else -remaining
            outstanding_total += signed_remaining
            outstanding_rows.append({
                "id": doc.id, "date": doc.transaction_date,
                "document_number": doc.document_number, "description": doc.description,
                "amount": str(signed_remaining), "original_amount": str(signed),
                "allocated_amount": str(allocated), "remaining_amount": str(remaining),
                "timing_difference_days": (recon.statement_date - doc.transaction_date).days,
            })
        unmatched_statement_total = Decimal("0")
        matched_lines = 0
        unmatched_lines = 0
        for line in lines:
            allocated = _allocated_line_amount(line)
            remaining = max(Decimal("0"), abs(line.amount) - allocated)
            if remaining == 0:
                matched_lines += 1
            else:
                unmatched_lines += 1
                unmatched_statement_total += remaining if line.amount > 0 else -remaining
        expected_book = recon.closing_balance + outstanding_total - unmatched_statement_total
        return response.Response({
            "reconciliation": self.get_serializer(recon).data,
            "statement_calculated": str(statement_balance),
            "statement_difference": str(recon.closing_balance - statement_balance),
            "book_balance": str(book_balance),
            "outstanding_total": str(outstanding_total.quantize(Decimal("0.0001"))),
            "unmatched_statement_total": str(unmatched_statement_total),
            "expected_book_balance": str(expected_book),
            "unexplained_difference": str(book_balance - expected_book),
            "matched_lines": matched_lines,
            "unmatched_lines": unmatched_lines,
            "lines": BankStatementLineSerializer(lines, many=True).data,
            "outstanding_transactions": outstanding_rows,
        })

    @transaction.atomic
    @decorators.action(detail=True, methods=("post",))
    def reconcile(self, request, pk=None):
        """اعتماد التسوية بعد استنفاد كل مبالغ سطور الكشف والتحقق من الفروق."""
        scoped = self.get_object()
        recon = BankReconciliation.objects.select_for_update().get(pk=scoped.pk)
        self._write(recon.company_id)
        if recon.bank_account.branch_id and not can_access_branch(
            request.user, recon.bank_account.branch_id, recon.company_id
        ):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الحساب البنكي.")
        if recon.status != BankReconciliation.Status.DRAFT:
            raise ValidationError("التسوية معتمدة بالفعل.")
        lines = list(recon.lines.select_for_update().all())
        unmatched_total = Decimal("0")
        for line in lines:
            remaining = abs(line.amount) - _allocated_line_amount(line)
            if remaining > 0:
                unmatched_total += remaining
        if unmatched_total > 0:
            raise ValidationError("لا يمكن الاعتماد قبل استنفاد كامل قيمة جميع سطور كشف البنك.")
        line_total = recon.opening_balance + (recon.lines.aggregate(total=Sum("amount"))["total"] or Decimal("0"))
        if line_total != recon.closing_balance:
            raise ValidationError("رصيد الإقفال لا يساوي الرصيد الافتتاحي مضافًا إليه صافي حركات الكشف.")
        branch_ids = accessible_branches(request.user, recon.company_id).values_list("id", flat=True)
        if recon.bank_account.branch_id:
            branch_ids = [recon.bank_account.branch_id]
        book_balance = _bank_book_balance(recon.bank_account, recon.statement_date, branch_ids)
        outstanding = TreasuryTransaction.objects.filter(
            company_id=recon.company_id, branch_id__in=branch_ids,
            status=TreasuryTransaction.Status.POSTED,
            transaction_date__lte=recon.statement_date,
        ).filter(
            models.Q(source_account=recon.bank_account)
            | models.Q(destination_account=recon.bank_account)
        )
        outstanding_total = Decimal("0")
        for doc in outstanding:
            if doc.reconciliation_allocations.filter(
                line__reconciliation__bank_account=recon.bank_account,
            ).exclude(line__reconciliation=recon).exists():
                continue
            signed = _signed_bank_transaction_amount(doc, recon.bank_account)
            remaining = abs(signed) - _allocated_transaction_amount(doc, recon.bank_account)
            if remaining > 0:
                outstanding_total += remaining if signed > 0 else -remaining
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


class TreasuryChequeViewSet(TreasuryBaseViewSet):
    serializer_class = TreasuryChequeSerializer
    queryset = TreasuryCheque.objects.select_related(
        "company", "branch", "fiscal_year", "bank_account", "treasury_transaction"
    ).all()
    filterset_fields = ("company", "branch", "bank_account", "direction", "status", "due_date")
    search_fields = ("cheque_number", "counterparty", "notes")
    ordering_fields = ("issue_date", "due_date", "amount", "cheque_number", "created_at")

    def get_queryset(self):
        qs = self.queryset.filter(company__in=accessible_companies(self.request.user))
        if not self.request.user.is_superuser or is_platform_owner(self.request.user):
            qs = qs.filter(branch__in=accessible_branches(self.request.user))
        return qs

    @transaction.atomic
    def perform_create(self, serializer):
        data = serializer.validated_data
        self._scope(data["company"].pk)
        self._write(data["company"].pk)
        if not can_access_branch(self.request.user, data["branch"].pk, data["company"].pk):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الشيك.")
        direction = data["direction"]
        status_value = TreasuryCheque.Status.RECEIVED if direction == TreasuryCheque.Direction.INCOMING else TreasuryCheque.Status.ISSUED
        serializer.save(created_by=self.request.user, status=status_value)

    def perform_update(self, serializer):
        cheque = serializer.instance
        self._write(cheque.company_id)
        if cheque.status in (TreasuryCheque.Status.CLEARED, TreasuryCheque.Status.BOUNCED, TreasuryCheque.Status.CANCELLED):
            raise ValidationError("لا يمكن تعديل شيك تمت تسويته أو ارتداده أو إلغاؤه.")
        if not can_access_branch(self.request.user, cheque.branch_id, cheque.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الشيك.")
        if "status" in serializer.validated_data or "treasury_transaction" in serializer.validated_data:
            raise ValidationError("تغيير حالة الشيك أو حركته يتم من إجراءات دورة الشيك وليس بالتعديل المباشر.")
        serializer.save()

    @decorators.action(detail=True, methods=("post",))
    @transaction.atomic
    def deposit(self, request, pk=None):
        cheque = self.get_object()
        self._write(cheque.company_id)
        if cheque.direction != TreasuryCheque.Direction.INCOMING or cheque.status != TreasuryCheque.Status.RECEIVED:
            raise ValidationError("الإيداع متاح للشيك الوارد المستلم فقط.")
        deposit_date = dateparse.parse_date(str(request.data.get("deposit_date", "")))
        if not deposit_date or deposit_date < cheque.issue_date:
            raise ValidationError({"deposit_date": "أدخل تاريخ إيداع صحيحًا لا يسبق تاريخ الشيك."})
        cheque.status = TreasuryCheque.Status.DEPOSITED
        cheque.deposit_date = deposit_date
        cheque.save(update_fields=("status", "deposit_date", "updated_at"))
        return response.Response(self.get_serializer(cheque).data)

    @decorators.action(detail=True, methods=("post",))
    @transaction.atomic
    def under_collection(self, request, pk=None):
        cheque = self.get_object()
        self._write(cheque.company_id)
        if cheque.direction != TreasuryCheque.Direction.INCOMING or cheque.status != TreasuryCheque.Status.DEPOSITED:
            raise ValidationError("حالة تحت التحصيل تتطلب شيكًا واردًا تم إيداعه.")
        cheque.status = TreasuryCheque.Status.UNDER_COLLECTION
        cheque.save(update_fields=("status", "updated_at"))
        return response.Response(self.get_serializer(cheque).data)

    @decorators.action(detail=True, methods=("post",))
    @transaction.atomic
    def present(self, request, pk=None):
        cheque = self.get_object()
        self._write(cheque.company_id)
        if cheque.direction != TreasuryCheque.Direction.OUTGOING or cheque.status != TreasuryCheque.Status.ISSUED:
            raise ValidationError("تقديم الشيك للصرف متاح للشيك الصادر فقط.")
        cheque.status = TreasuryCheque.Status.PRESENTED
        cheque.save(update_fields=("status", "updated_at"))
        return response.Response(self.get_serializer(cheque).data)

    @decorators.action(detail=True, methods=("post",))
    @transaction.atomic
    def clear(self, request, pk=None):
        cheque = self.get_object()
        self._write(cheque.company_id)
        allowed = (
            cheque.direction == TreasuryCheque.Direction.INCOMING
            and cheque.status in (TreasuryCheque.Status.DEPOSITED, TreasuryCheque.Status.UNDER_COLLECTION)
        ) or (
            cheque.direction == TreasuryCheque.Direction.OUTGOING
            and cheque.status in (TreasuryCheque.Status.ISSUED, TreasuryCheque.Status.PRESENTED)
        )
        if not allowed:
            raise ValidationError("لا يمكن تسوية الشيك من حالته الحالية.")
        raw_id = request.data.get("treasury_transaction")
        if not raw_id or not str(raw_id).isdigit():
            raise ValidationError({"treasury_transaction": "اربط سند خزينة بنكيًا مرحّلًا لإثبات التحصيل أو الصرف."})
        doc = TreasuryTransaction.objects.select_for_update().filter(
            pk=int(raw_id), company=cheque.company, source_account=cheque.bank_account,
            amount=cheque.amount, status=TreasuryTransaction.Status.POSTED,
            kind=TreasuryTransaction.Kind.RECEIPT if cheque.direction == TreasuryCheque.Direction.INCOMING else TreasuryTransaction.Kind.PAYMENT,
        ).first()
        if not doc:
            raise ValidationError({"treasury_transaction": "سند الخزينة غير مرحّل أو لا يطابق البنك أو المبلغ أو اتجاه الشيك."})
        if TreasuryCheque.objects.filter(treasury_transaction=doc).exclude(pk=cheque.pk).exists():
            raise ValidationError({"treasury_transaction": "سند الخزينة مرتبط بشيك آخر."})
        clearing_date = dateparse.parse_date(str(request.data.get("clearing_date", "")))
        if not clearing_date or clearing_date < cheque.issue_date:
            raise ValidationError({"clearing_date": "أدخل تاريخ تحصيل/صرف صحيحًا."})
        cheque.treasury_transaction = doc
        cheque.clearing_date = clearing_date
        cheque.status = TreasuryCheque.Status.CLEARED
        cheque.save(update_fields=("treasury_transaction", "clearing_date", "status", "updated_at"))
        return response.Response(self.get_serializer(cheque).data)

    @decorators.action(detail=True, methods=("post",))
    @transaction.atomic
    def bounce(self, request, pk=None):
        cheque = self.get_object()
        self._write(cheque.company_id)
        if cheque.status in (TreasuryCheque.Status.CLEARED, TreasuryCheque.Status.BOUNCED, TreasuryCheque.Status.CANCELLED):
            raise ValidationError("لا يمكن ارتداد شيك تمت تسويته أو ارتداده أو إلغاؤه؛ عالج القيد المرتبط أولًا.")
        if cheque.direction == TreasuryCheque.Direction.OUTGOING and cheque.treasury_transaction_id:
            if cheque.treasury_transaction.status != TreasuryTransaction.Status.REVERSED:
                raise ValidationError("اعكس سند الصرف المرتبط بالشيك الصادر أولًا من إجراء العكس المحاسبي.")
        bounce_date = dateparse.parse_date(str(request.data.get("bounce_date", "")))
        if not bounce_date:
            raise ValidationError({"bounce_date": "أدخل تاريخ ارتداد صحيحًا."})
        cheque.status = TreasuryCheque.Status.BOUNCED
        cheque.bounce_date = bounce_date
        cheque.notes = (cheque.notes + " | " if cheque.notes else "") + str(request.data.get("reason", "شيك مرتد"))[:300]
        cheque.save(update_fields=("status", "bounce_date", "notes", "updated_at"))
        return response.Response(self.get_serializer(cheque).data)

    @decorators.action(detail=True, methods=("post",))
    @transaction.atomic
    def cancel(self, request, pk=None):
        cheque = self.get_object()
        self._write(cheque.company_id)
        if cheque.status not in (TreasuryCheque.Status.RECEIVED, TreasuryCheque.Status.ISSUED):
            raise ValidationError("الإلغاء متاح للشيك المستلم أو الصادر قبل الإيداع أو التقديم فقط.")
        if cheque.direction == TreasuryCheque.Direction.OUTGOING and cheque.treasury_transaction.status != TreasuryTransaction.Status.REVERSED:
            raise ValidationError("اعكس سند الصرف المرتبط أولًا قبل إلغاء الشيك.")
        cheque.status = TreasuryCheque.Status.CANCELLED
        cheque.save(update_fields=("status", "updated_at"))
        return response.Response(self.get_serializer(cheque).data)


class CashCommitmentViewSet(TreasuryBaseViewSet):
    serializer_class = CashCommitmentSerializer
    queryset = CashCommitment.objects.select_related("company", "branch", "payable_account").prefetch_related(
        "payments__journal_entry", "payments__reversal_journal_entry"
    ).all()
    filterset_fields = ("company", "branch", "commitment_type", "status", "due_date")
    ordering_fields = ("due_date", "created_at", "amount", "base_amount")

    def get_queryset(self):
        qs = self.queryset.filter(company__in=accessible_companies(self.request.user))
        branch = self.request.query_params.get("branch")
        if branch:
            if not branch.isdigit():
                raise ValidationError({"branch": "الفرع المحدد غير صحيح."})
            branch_id = int(branch)
            qs = qs.filter(models.Q(branch_id=branch_id) | models.Q(branch__isnull=True))
            if not can_access_branch(self.request.user, branch_id):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")
        return qs

    def perform_create(self, serializer):
        company = serializer.validated_data["company"]
        self._scope(company.pk)
        self._write(company.pk)
        branch = serializer.validated_data.get("branch")
        if branch and not can_access_branch(self.request.user, branch.pk, company.pk):
            raise PermissionDenied("لا تملك صلاحية إدارة الالتزامات في هذا الفرع.")
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
        commitment = serializer.instance
        self._write(commitment.company_id)
        if commitment.status == CashCommitment.Status.VOID or commitment.payments.exists():
            raise ValidationError("لا يمكن تعديل التزام ملغي أو له دفعات مسجلة.")
        company = serializer.validated_data.get("company", commitment.company)
        if company.pk != commitment.company_id:
            raise ValidationError("لا يمكن نقل الالتزام بين الشركات.")
        branch = serializer.validated_data.get("branch", commitment.branch)
        if branch and not can_access_branch(self.request.user, branch.pk, commitment.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الالتزام.")
        serializer.save()

    def perform_destroy(self, instance):
        self._write(instance.company_id)
        if instance.status == CashCommitment.Status.VOID:
            raise ValidationError("الالتزام ملغي بالفعل.")
        if self._paid_total(instance) > 0:
            raise ValidationError("لا يمكن إلغاء التزام عليه دفعات فعالة.")
        instance.status = CashCommitment.Status.VOID
        instance.save(update_fields=("status", "updated_at"))

    @staticmethod
    def _paid_total(commitment):
        from accounting.models import JournalEntry
        return sum((
            payment.base_amount for payment in commitment.payments.select_related("reversal_journal_entry").all()
            if not payment.reversal_journal_entry_id
            or payment.reversal_journal_entry.status == JournalEntry.Status.REVERSED
        ), Decimal("0"))

    def _refresh_status(self, commitment):
        paid = self._paid_total(commitment)
        if commitment.status != CashCommitment.Status.VOID:
            commitment.status = (
                CashCommitment.Status.PAID if paid >= commitment.base_amount
                else CashCommitment.Status.PARTIALLY_PAID if paid > 0
                else CashCommitment.Status.OPEN
            )
            commitment.save(update_fields=("status", "updated_at"))

    @transaction.atomic
    @decorators.action(detail=True, methods=("post",), url_path="record-payment")
    def record_payment(self, request, pk=None):
        """ربط دفعة بقيد سداد مرحّل ومخصص للالتزام دون إنشاء قيد جديد."""
        from accounting.models import JournalEntry, JournalLine
        commitment = self.get_object()
        self._scope(commitment.company_id)
        self._write(commitment.company_id)
        if commitment.status == CashCommitment.Status.VOID:
            raise ValidationError("لا يمكن سداد التزام ملغي.")
        journal_id = request.data.get("journal_entry")
        payment_date = dateparse.parse_date(str(request.data.get("payment_date", "")))
        try:
            amount = Decimal(str(request.data.get("amount", "")))
        except (InvalidOperation, TypeError, ValueError):
            raise ValidationError({"amount": "أدخل مبلغًا صحيحًا."}) from None
        if not journal_id or not str(journal_id).isdigit() or not payment_date or not amount.is_finite() or amount <= 0:
            raise ValidationError({"detail": "أدخل قيد السداد وتاريخه ومبلغًا موجبًا صحيحًا."})
        journal = JournalEntry.objects.select_for_update().filter(
            pk=int(journal_id), company_id=commitment.company_id, status=JournalEntry.Status.POSTED
        ).first()
        if not journal:
            raise ValidationError({"journal_entry": "يجب اختيار قيد يومية مرحّل تابع للشركة."})
        if journal.entry_date != payment_date:
            raise ValidationError({"payment_date": "تاريخ السداد يجب أن يطابق تاريخ قيد اليومية."})
        lines = list(JournalLine.objects.filter(entry=journal).select_related("account"))
        debit_total = sum((line.debit for line in lines), Decimal("0"))
        credit_total = sum((line.credit for line in lines), Decimal("0"))
        payable_debit = sum((line.debit for line in lines if line.account_id == commitment.payable_account_id), Decimal("0"))
        if payable_debit != amount or debit_total != amount or credit_total != amount:
            raise ValidationError("يجب أن يكون القيد مخصصًا لهذه الدفعة وحدها، ومدين حساب الالتزام وإجمالي المدين والدائن يساوي المبلغ.")
        if amount > commitment.base_amount - self._paid_total(commitment):
            raise ValidationError({"amount": "قيمة الدفعة تتجاوز الرصيد المتبقي من الالتزام."})
        payment = CashCommitmentPayment.objects.create(
            commitment=commitment, payment_date=payment_date, amount=amount,
            base_amount=amount, journal_entry=journal,
            reference=str(request.data.get("reference", ""))[:120],
            notes=str(request.data.get("notes", ""))[:500], created_by=request.user,
        )
        self._refresh_status(commitment)
        return response.Response(CashCommitmentPaymentSerializer(payment).data, status=status.HTTP_201_CREATED)

    @transaction.atomic
    @decorators.action(detail=True, methods=("post",), url_path="reverse-payment")
    def reverse_payment(self, request, pk=None):
        """ربط قيد عكسي مرحّل بالسداد لإعادة الالتزام إلى رصيده المستحق."""
        from accounting.models import JournalEntry, JournalLine
        commitment = self.get_object()
        self._scope(commitment.company_id)
        self._write(commitment.company_id)
        payment_id = request.data.get("payment_id")
        reversal_id = request.data.get("reversal_journal_entry")
        payment = commitment.payments.select_for_update().filter(pk=payment_id).first()
        if not payment or payment.reversal_journal_entry_id:
            raise ValidationError({"payment_id": "دفعة غير موجودة أو سبق ربط عكس بها."})
        reversal = JournalEntry.objects.select_for_update().filter(
            pk=reversal_id, company_id=commitment.company_id, status=JournalEntry.Status.POSTED
        ).first()
        if not reversal:
            raise ValidationError({"reversal_journal_entry": "اختر قيد عكس مرحّل تابع للشركة."})
        lines = list(JournalLine.objects.filter(entry=reversal))
        credit_total = sum((line.credit for line in lines), Decimal("0"))
        debit_total = sum((line.debit for line in lines), Decimal("0"))
        payable_credit = sum((line.credit for line in lines if line.account_id == commitment.payable_account_id), Decimal("0"))
        if payable_credit != payment.base_amount or debit_total != payment.base_amount or credit_total != payment.base_amount:
            raise ValidationError("قيد العكس يجب أن يعيد نفس مبلغ الالتزام إلى حساب الخصوم.")
        payment.reversal_journal_entry = reversal
        payment.save(update_fields=("reversal_journal_entry",))
        self._refresh_status(commitment)
        return response.Response(CashCommitmentPaymentSerializer(payment).data)

    @decorators.action(detail=True, methods=("post",), url_path="void")
    def void(self, request, pk=None):
        commitment = self.get_object()
        self._write(commitment.company_id)
        if commitment.status == CashCommitment.Status.VOID or self._paid_total(commitment) > 0:
            raise ValidationError("لا يمكن إلغاء التزام ملغي بالفعل أو عليه دفعات فعالة.")
        commitment.status = CashCommitment.Status.VOID
        commitment.save(update_fields=("status", "updated_at"))
        return response.Response(self.get_serializer(commitment).data)


class CashCommitmentScheduleViewSet(TreasuryBaseViewSet):
    serializer_class = CashCommitmentScheduleSerializer
    queryset = CashCommitmentSchedule.objects.select_related("company", "branch", "payable_account").all()
    filterset_fields = ("company", "branch", "frequency", "active")

    def get_queryset(self):
        return self.queryset.filter(company__in=accessible_companies(self.request.user))

    def perform_create(self, serializer):
        company = serializer.validated_data["company"]
        self._scope(company.pk)
        self._write(company.pk)
        branch = serializer.validated_data.get("branch")
        if branch and not can_access_branch(self.request.user, branch.pk, company.pk):
            raise PermissionDenied("لا تملك صلاحية إدارة الجدول الدوري في هذا الفرع.")
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
        schedule = serializer.instance
        self._write(schedule.company_id)
        if schedule.occurrences.filter(commitment__isnull=False).exists():
            raise ValidationError("لا يمكن تعديل جدول بعد توليد استحقاقاته؛ أوقفه وأنشئ جدولًا جديدًا.")
        serializer.save()

    @transaction.atomic
    @decorators.action(detail=True, methods=("post",), url_path="generate")
    def generate(self, request, pk=None):
        """توليد الاستحقاقات حتى تاريخ محدد مع منع تكرار أي شهر."""
        from calendar import monthrange
        from datetime import date as date_type
        schedule = self.get_object()
        self._write(schedule.company_id)
        if schedule.branch_id and not can_access_branch(request.user, schedule.branch_id, schedule.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الجدول.")
        through_date = dateparse.parse_date(str(request.data.get("through_date", "")))
        if not through_date:
            raise ValidationError({"through_date": "أدخل تاريخًا صحيحًا لتوليد الاستحقاقات حتى تاريخه."})
        if not schedule.active:
            raise ValidationError("الجدول الدوري متوقف.")
        next_due = schedule.next_due_date
        generated = 0
        while next_due <= through_date and (not schedule.end_date or next_due <= schedule.end_date):
            occurrence, created = CashCommitmentOccurrence.objects.get_or_create(
                schedule=schedule, due_date=next_due,
            )
            if created:
                commitment = CashCommitment.objects.create(
                    company=schedule.company, branch=schedule.branch,
                    commitment_type=CashCommitment.Kind.RECURRING_EXPENSE,
                    title=schedule.title, period_label=next_due.strftime("%Y-%m"),
                    amount=schedule.amount, currency_code=schedule.currency_code,
                    exchange_rate=schedule.exchange_rate, base_amount=schedule.base_amount,
                    due_date=next_due, payable_account=schedule.payable_account,
                    origin_type="recurring_schedule_occurrence", origin_id=occurrence.pk,
                    notes=schedule.notes, created_by=request.user,
                )
                occurrence.commitment = commitment
                occurrence.save(update_fields=("commitment",))
                generated += 1
            month_index = next_due.month - 1 + schedule.interval_months
            year = next_due.year + month_index // 12
            month = month_index % 12 + 1
            day = min(schedule.anchor_day, monthrange(year, month)[1])
            next_due = date_type(year, month, day)
        schedule.next_due_date = next_due
        if schedule.end_date and next_due > schedule.end_date:
            schedule.active = False
        schedule.save(update_fields=("next_due_date", "active", "updated_at"))
        return response.Response({
            "generated": generated, "next_due_date": schedule.next_due_date,
            "active": schedule.active,
        })
