"""Customer advance refund posting and reversal services."""
from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from accounting.models import AccountRole, FiscalYear, JournalEntry, JournalLine
from accounting.services import get_account_for_role, post_journal, reverse_operational_journal
from audit.models import AuditEvent
from sales.models import CustomerRefund, CustomerReceiptBatch, CustomerReceiptBatchAllocation
from treasury.models import TreasuryAccount, TreasuryTransaction


def _available_advance(batch, as_of):
    allocated = CustomerReceiptBatchAllocation.objects.filter(
        batch=batch, allocation_date__lte=as_of,
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0")
    refunds = CustomerRefund.objects.filter(
        source_receipt_batch=batch, refund_date__lte=as_of,
    ).filter(
        Q(status=CustomerRefund.Status.POSTED)
        | Q(status=CustomerRefund.Status.REVERSED, reversal_date__gt=as_of)
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0")
    return max(batch.amount - allocated - refunds, Decimal("0")).quantize(Decimal("0.0001"))


@transaction.atomic
def post_customer_refund(refund_id, user):
    """Post a refund against the customer's still-unallocated advance."""
    refund = CustomerRefund.objects.select_for_update(of=("self",)).select_related(
        "company__base_currency", "customer", "branch", "source_receipt_batch",
        "treasury_account__account",
    ).get(pk=refund_id)
    if refund.status != CustomerRefund.Status.DRAFT:
        raise ValidationError("مردود العميل مرحّل بالفعل.")
    batch = CustomerReceiptBatch.objects.select_for_update(of=("self",)).select_related(
        "company__base_currency", "customer", "branch", "journal_entry",
    ).get(pk=refund.source_receipt_batch_id)
    if batch.status != CustomerReceiptBatch.Status.POSTED or not batch.journal_entry_id:
        raise ValidationError("لا يمكن رد مبلغ إلا من سند قبض مجمع مرحّل.")
    if batch.journal_entry.status != JournalEntry.Status.POSTED:
        raise ValidationError("القيد الأصلي لسند القبض ليس مرحّلًا.")
    if refund.company_id != batch.company_id or refund.customer_id != batch.customer_id or refund.branch_id != batch.branch_id:
        raise ValidationError("بيانات المردود لا تطابق سند القبض الأصلي.")
    if not refund.customer.is_active or not refund.branch.is_active:
        raise ValidationError("لا يمكن رد مبلغ لعميل أو فرع موقوف.")
    if refund.refund_date < batch.receipt_date:
        raise ValidationError("تاريخ المردود لا يجوز أن يسبق تاريخ سند القبض.")
    if not refund.amount.is_finite() or refund.amount <= 0 or refund.amount >= Decimal("10000000000000000") or refund.amount != refund.amount.quantize(Decimal("0.0001")):
        raise ValidationError("مبلغ المردود يجب أن يكون موجبًا وبحد أقصى 4 منازل عشرية.")
    available = _available_advance(batch, refund.refund_date)
    if refund.amount > available:
        raise ValidationError(f"مبلغ المردود يتجاوز الدفعة المقدمة غير المخصصة المتاحة ({available}).")

    treasury = TreasuryAccount.objects.select_for_update(of=("self",)).select_related(
        "account", "company__base_currency",
    ).get(pk=refund.treasury_account_id)
    expected_kind = TreasuryAccount.Kind.CASH if treasury.kind == TreasuryAccount.Kind.CASH else TreasuryAccount.Kind.BANK
    if (
        treasury.company_id != refund.company_id or not treasury.is_active
        or treasury.kind != expected_kind or treasury.account.nature != "asset"
        or not treasury.account.is_active or treasury.account.is_group or not treasury.account.allow_posting
        or (treasury.branch_id and treasury.branch_id != refund.branch_id)
        or treasury.currency_code != refund.company.base_currency.code
    ):
        raise ValidationError("حساب الخزينة/البنك غير نشط أو لا يطابق الشركة والفرع والعملة.")
    receivable = get_account_for_role(refund.company, AccountRole.Role.ACCOUNTS_RECEIVABLE)
    if receivable.pk == treasury.account_id:
        raise ValidationError("حساب العملاء يجب أن يختلف عن حساب الخزينة/البنك.")
    year = FiscalYear.objects.filter(
        company=refund.company, status=FiscalYear.Status.OPEN,
        start_date__lte=refund.refund_date, end_date__gte=refund.refund_date,
    ).order_by("-start_date").first()
    if not year:
        raise ValidationError("لا توجد سنة مالية مفتوحة تشمل تاريخ المردود.")

    entry = JournalEntry.objects.create(
        company=refund.company, branch=refund.branch, fiscal_year=year,
        entry_number=f"SALE-REF-{refund.pk}", entry_date=refund.refund_date,
        description=f"رد دفعة مقدمة للعميل {refund.customer.name_ar} رقم {refund.refund_number}"[:500],
        source_type=JournalEntry.Source.SALES, created_by=user,
    )
    JournalLine.objects.bulk_create([
        JournalLine(entry=entry, account=receivable, description="رد جزء من الدفعة المقدمة للعميل", debit=refund.amount, credit=0),
        JournalLine(entry=entry, account=treasury.account, description="صرف مبلغ مردود للعميل", debit=0, credit=refund.amount),
    ])
    try:
        posted = post_journal(entry.pk, actor=user)
    except DjangoValidationError as exc:
        detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
        raise ValidationError(detail) from exc

    from treasury.services import record_posted_document_bank_movement
    movement = record_posted_document_bank_movement(
        company=refund.company, branch=refund.branch, fiscal_year=year,
        account=treasury.account, kind=TreasuryTransaction.Kind.PAYMENT,
        amount=refund.amount, transaction_date=refund.refund_date,
        document_number=f"AUTO-SALE-REF-{refund.pk}",
        description=f"رد مبلغ للعميل {refund.customer.name_ar} - سند {refund.refund_number}",
        counter_account=receivable, journal_entry=posted, user=user, reference=batch.reference,
    )
    if movement is None:
        raise ValidationError("تعذر إنشاء حركة الخزينة المرتبطة بالمردود.")
    refund.treasury_transaction = movement
    refund.status = CustomerRefund.Status.POSTED
    refund.save(update_fields=("treasury_transaction", "status"))
    AuditEvent.objects.create(
        actor=user, company=refund.company, branch=refund.branch,
        action=AuditEvent.Action.ACTION, resource="sales.customer_refund.posted",
        object_id=str(refund.pk), http_method="POST", status_code=200, succeeded=True,
        metadata={"refund_number": refund.refund_number, "amount": str(refund.amount),
                  "source_receipt_batch_id": batch.pk, "journal_entry_id": posted.pk,
                  "treasury_transaction_id": movement.pk},
    )
    return refund


@transaction.atomic
def reverse_customer_refund(refund_id, user, entry_date):
    """Reverse a posted customer advance refund and restore the advance."""
    refund = CustomerRefund.objects.select_for_update(of=("self",)).select_related(
        "company", "customer", "branch", "source_receipt_batch",
        "treasury_transaction__journal_entry",
    ).get(pk=refund_id)
    if refund.status != CustomerRefund.Status.POSTED or not refund.treasury_transaction_id:
        raise ValidationError("لا يمكن عكس إلا مردود عميل مرحّل.")
    movement = refund.treasury_transaction
    original_entry = movement.journal_entry
    if not original_entry or original_entry.status != JournalEntry.Status.POSTED:
        raise ValidationError("القيد الأصلي لمردود العميل ليس مرحّلًا.")
    if entry_date < refund.refund_date:
        raise ValidationError("تاريخ العكس لا يجوز أن يسبق تاريخ المردود.")
    if CustomerReceiptBatchAllocation.objects.filter(
        batch=refund.source_receipt_batch, allocation_date__gt=refund.refund_date,
    ).exists() or CustomerRefund.objects.filter(
        source_receipt_batch=refund.source_receipt_batch,
        status=CustomerRefund.Status.POSTED,
        refund_date__gt=refund.refund_date,
    ).exclude(pk=refund.pk).exists():
        raise ValidationError("لا يمكن عكس المردود لوجود تخصيص أو مردود أحدث على الدفعة؛ اعكس الحركات الأحدث أولًا.")
    cash_totals = JournalLine.objects.filter(
        account_id=refund.treasury_account.account_id,
        entry__entry_date__lte=entry_date,
        entry__status__in=(JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED),
    ).aggregate(debit=Sum("debit"), credit=Sum("credit"))
    opening = TreasuryAccount.objects.filter(
        pk=refund.treasury_account_id, opening_journal_entry__isnull=True,
    ).aggregate(total=Sum("opening_balance"))["total"] or Decimal("0")
    cash_balance = opening + (cash_totals["debit"] or Decimal("0")) - (cash_totals["credit"] or Decimal("0"))
    if cash_balance < refund.amount:
        raise ValidationError(f"لا يمكن عكس المردود: رصيد الخزينة/البنك ({cash_balance}) أقل من المبلغ ({refund.amount}).")
    try:
        reversal = reverse_operational_journal(
            original_entry.pk, user, entry_date, JournalEntry.Source.SALES,
        )
        from treasury.services import reverse_linked_document_treasury_movement
        reverse_linked_document_treasury_movement(
            journal_entry_id=original_entry.pk, reversal_journal_entry=reversal, user=user,
        )
    except DjangoValidationError as exc:
        detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
        raise ValidationError(detail) from exc
    reversal_movement = TreasuryTransaction.objects.create(
        company=refund.company, branch=refund.branch, fiscal_year=reversal.fiscal_year,
        source_account=refund.treasury_account, counter_account=movement.counter_account,
        kind=TreasuryTransaction.Kind.RECEIPT, status=TreasuryTransaction.Status.POSTED,
        document_number=f"AUTO-SALE-REF-REV-{refund.pk}",
        transaction_date=entry_date, amount=refund.amount,
        description=f"عكس رد مبلغ العميل {refund.customer.name_ar} - سند {refund.refund_number}"[:500],
        reference=movement.reference, journal_entry=reversal, created_by=user,
        posted_at=timezone.now(),
    )
    refund.status = CustomerRefund.Status.REVERSED
    refund.reversal_date = entry_date
    refund.reversal_journal_entry = reversal
    refund.reversal_treasury_transaction = reversal_movement
    refund.save(update_fields=("status", "reversal_date", "reversal_journal_entry", "reversal_treasury_transaction"))
    AuditEvent.objects.create(
        actor=user, company=refund.company, branch=refund.branch,
        action=AuditEvent.Action.ACTION, resource="sales.customer_refund.reversed",
        object_id=str(refund.pk), http_method="POST", status_code=200, succeeded=True,
        metadata={"refund_number": refund.refund_number, "reversal_date": entry_date.isoformat(),
                  "reversal_journal_entry_id": reversal.pk,
                  "reversal_treasury_transaction_id": reversal_movement.pk},
    )
    return refund
