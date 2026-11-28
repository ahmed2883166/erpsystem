from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from accounting.models import AccountRole, FiscalYear, JournalEntry, JournalLine
from accounting.services import get_account_for_role, post_journal, reverse_operational_journal
from audit.models import AuditEvent
from companies.access import resolve_warehouse_branch
from sales.models import (
    CustomerReceipt, CustomerReceiptBatch, CustomerReceiptBatchAllocation,
    CustomerRefund, SalesInvoice, SalesReturn,
)
from treasury.models import TreasuryAccount, TreasuryTransaction


def _account(company, role):
    return get_account_for_role(company, role)


def _invoice_open_balance(invoice, as_of):
    direct_paid = CustomerReceipt.objects.filter(
        invoice=invoice, receipt_date__lte=as_of,
    ).filter(
        Q(status=CustomerReceipt.Status.POSTED)
        | Q(status=CustomerReceipt.Status.REVERSED, journal_entry__reversal_entry__entry_date__gt=as_of)
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0")
    batch_allocations = CustomerReceiptBatchAllocation.objects.filter(
        invoice=invoice, allocation_date__lte=as_of,
    ).filter(
        Q(batch__status=CustomerReceiptBatch.Status.POSTED)
        | Q(batch__status=CustomerReceiptBatch.Status.REVERSED, batch__reversal_date__gt=as_of)
    )
    batch_paid = batch_allocations.aggregate(total=Sum("amount"))["total"] or Decimal("0")
    returned = SalesReturn.objects.filter(
        invoice=invoice, return_date__lte=as_of,
    ).filter(
        Q(status=SalesReturn.Status.POSTED)
        | Q(status=SalesReturn.Status.REVERSED, reversal_date__gt=as_of)
    ).aggregate(total=Sum("grand_total"))["total"] or Decimal("0")
    return max(invoice.grand_total - direct_paid - batch_paid - returned, Decimal("0"))


def _check_allocation_target(batch, invoice, amount, allocation_date=None):
    allocation_date = allocation_date or batch.receipt_date
    if invoice.status != SalesInvoice.Status.POSTED:
        raise ValidationError("لا يمكن تخصيص سند القبض إلا لفاتورة مبيعات مرحلة.")
    if invoice.company_id != batch.company_id or invoice.customer_id != batch.customer_id:
        raise ValidationError("الفاتورة لا تتبع العميل والشركة المحددين في سند القبض.")
    if invoice.transaction_currency_id:
        raise ValidationError("التخصيص المجمع متاح لفواتير العملة الأساسية فقط.")
    if invoice.invoice_date > allocation_date:
        raise ValidationError("تاريخ التخصيص لا يجوز أن يسبق تاريخ الفاتورة.")
    invoice_branch = (
        resolve_warehouse_branch(batch.company, invoice.delivery.warehouse) if invoice.delivery_id else invoice.branch
    )
    if not invoice_branch or invoice_branch.pk != batch.branch_id:
        raise ValidationError("الفاتورة لا تتبع فرع سند القبض.")
    remaining = _invoice_open_balance(invoice, allocation_date)
    if amount > remaining:
        raise ValidationError(
            f"التخصيص يتجاوز المتبقي على الفاتورة {invoice.invoice_number} ({remaining})."
        )
    later_direct = CustomerReceipt.objects.filter(
        invoice=invoice, status=CustomerReceipt.Status.POSTED,
        receipt_date__gt=allocation_date,
    ).exists()
    later_batch = CustomerReceiptBatchAllocation.objects.filter(
        invoice=invoice, batch__status=CustomerReceiptBatch.Status.POSTED,
        allocation_date__gt=allocation_date,
    ).exclude(batch=batch).exists()
    if later_direct or later_batch:
        raise ValidationError("لا يمكن تخصيص دفعة بتاريخ يسبق تحصيلًا أحدث على الفاتورة.")


@transaction.atomic
def post_customer_receipt_batch(batch_id, user):
    """ترحيل سند قبض مجمع واحد وإثبات الجزء غير المخصص كرصيد مقدم للعميل."""
    batch = CustomerReceiptBatch.objects.select_for_update(of=("self",)).select_related(
        "company", "customer", "branch", "payment_account",
    ).get(pk=batch_id)
    if batch.status != CustomerReceiptBatch.Status.DRAFT:
        raise ValidationError("سند القبض المجمع مرحّل بالفعل.")
    if not batch.customer.is_active:
        raise ValidationError("لا يمكن ترحيل سند قبض لعميل موقوف.")
    if not batch.branch_id or not batch.branch.is_active:
        raise ValidationError("يجب اختيار فرع نشط لسند القبض.")
    year = FiscalYear.objects.filter(
        company=batch.company, status=FiscalYear.Status.OPEN,
        start_date__lte=batch.receipt_date, end_date__gte=batch.receipt_date,
    ).order_by("-start_date").first()
    if not year:
        raise ValidationError("لا توجد سنة مالية مفتوحة تشمل تاريخ سند القبض.")

    allocations = list(
        batch.allocations.select_for_update(of=("self",)).select_related(
            "invoice", "invoice__delivery__warehouse", "invoice__branch",
        ).order_by("invoice_id", "id")
    )
    allocation_total = sum((row.amount for row in allocations), Decimal("0"))
    if allocation_total > batch.amount:
        raise ValidationError("إجمالي التخصيصات يتجاوز مبلغ سند القبض.")
    invoice_ids = sorted({row.invoice_id for row in allocations})
    invoices = {
        row.pk: row for row in SalesInvoice.objects.select_for_update(of=("self",)).filter(
            pk__in=invoice_ids,
        ).order_by("pk")
    }
    for allocation in allocations:
        invoice = invoices.get(allocation.invoice_id)
        if not invoice:
            raise ValidationError("إحدى الفواتير المخصصة غير موجودة.")
        _check_allocation_target(batch, invoice, allocation.amount, allocation.allocation_date)

    receivable = _account(batch.company, AccountRole.Role.ACCOUNTS_RECEIVABLE)
    cash_role = AccountRole.Role.CASH if batch.method == CustomerReceipt.Method.CASH else AccountRole.Role.BANK
    cash = batch.payment_account or _account(batch.company, cash_role)
    if (
        cash.company_id != batch.company_id or cash.nature != "asset"
        or not cash.is_active or cash.is_group or not cash.allow_posting
    ):
        raise ValidationError("حساب التحصيل يجب أن يكون حساب خزينة/بنك نشطًا وقابلًا للترحيل تابعًا للشركة.")
    if batch.payment_account_id:
        expected_kind = TreasuryAccount.Kind.CASH if batch.method == CustomerReceipt.Method.CASH else TreasuryAccount.Kind.BANK
        treasury_account = TreasuryAccount.objects.filter(
            company=batch.company, account=cash, kind=expected_kind, is_active=True,
        ).first()
        if (
            not treasury_account
            or (treasury_account.branch_id and treasury_account.branch_id != batch.branch_id)
            or treasury_account.currency_code != batch.company.base_currency.code
        ):
            raise ValidationError("حساب التحصيل المختار يجب أن يكون بعملة الشركة الأساسية ومطابقًا للفرع والطريقة.")
    if receivable.pk == cash.pk:
        raise ValidationError("حساب العملاء وحساب الصندوق/البنك يجب أن يكونا مختلفين.")

    entry = JournalEntry.objects.create(
        company=batch.company, branch=batch.branch, fiscal_year=year,
        entry_number=f"SALE-REC-BATCH-{batch.pk}", entry_date=batch.receipt_date,
        description=f"سند قبض مجمع للعميل {batch.customer.name_ar} رقم {batch.receipt_number}"[:500],
        source_type=JournalEntry.Source.SALES, created_by=user,
    )
    JournalLine.objects.bulk_create([
        JournalLine(
            entry=entry, account=cash, description="استلام نقدية/تحويل من العميل",
            debit=batch.amount, credit=0,
        ),
        JournalLine(
            entry=entry, account=receivable, description="تحصيل من العميل وتخصيصه للفواتير/دفعة مقدمة",
            debit=0, credit=batch.amount,
        ),
    ])
    try:
        posted = post_journal(entry.pk, actor=user)
    except DjangoValidationError as exc:
        detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
        raise ValidationError(detail) from exc

    if batch.method == CustomerReceipt.Method.BANK:
        from treasury.models import TreasuryTransaction
        from treasury.services import record_posted_document_bank_movement
        record_posted_document_bank_movement(
            company=batch.company, branch=batch.branch, fiscal_year=year, account=cash,
            kind=TreasuryTransaction.Kind.RECEIPT, amount=batch.amount,
            transaction_date=batch.receipt_date,
            document_number=f"AUTO-SALE-REC-BATCH-{batch.pk}",
            description=f"تحصيل عميل {batch.customer.name_ar} - سند {batch.receipt_number}",
            counter_account=receivable, journal_entry=posted, user=user, reference=batch.reference,
        )

    CustomerReceiptBatch.objects.filter(
        pk=batch.pk, status=CustomerReceiptBatch.Status.DRAFT,
    ).update(
        status=CustomerReceiptBatch.Status.POSTED,
        journal_entry=posted,
        posted_at=timezone.now(),
    )
    AuditEvent.objects.create(
        actor=user, company=batch.company, branch=batch.branch,
        action=AuditEvent.Action.ACTION, resource="sales.customer_receipt_batch.posted",
        object_id=str(batch.pk), http_method="POST", status_code=200, succeeded=True,
        metadata={
            "receipt_number": batch.receipt_number,
            "amount": str(batch.amount),
            "allocated_amount": str(allocation_total),
            "unallocated_amount": str(batch.amount - allocation_total),
            "allocation_count": len(allocations),
            "journal_entry_id": posted.pk,
        },
    )
    batch.refresh_from_db()
    return batch


@transaction.atomic
def allocate_customer_receipt_batch(batch_id, invoice_id, amount, user, allocation_date=None):
    """تخصيص دفعة مقدمة قائمة إلى فاتورة دون إنشاء حركة نقدية أو قيد جديد."""
    batch = CustomerReceiptBatch.objects.select_for_update(of=("self",)).select_related(
        "company", "customer", "branch",
    ).get(pk=batch_id)
    if batch.status != CustomerReceiptBatch.Status.POSTED:
        raise ValidationError("يمكن تخصيص الدفعة المقدمة من سند قبض مرحّل فقط.")
    invoice = SalesInvoice.objects.select_for_update(of=("self",)).select_related(
        "delivery__warehouse", "branch",
    ).get(pk=invoice_id)
    amount = Decimal(str(amount))
    if not amount.is_finite() or amount <= 0:
        raise ValidationError("أدخل مبلغ تخصيص صحيحًا وأكبر من صفر.")
    if amount >= Decimal("10000000000000000") or amount != amount.quantize(Decimal("0.0001")):
        raise ValidationError("مبلغ التخصيص يجب ألا يتجاوز 16 رقمًا صحيحًا و4 منازل عشرية.")
    if amount > batch.unallocated_amount:
        raise ValidationError(f"مبلغ التخصيص يتجاوز الدفعة المقدمة المتاحة ({batch.unallocated_amount}).")
    allocation_date = allocation_date or timezone.localdate()
    if allocation_date < batch.receipt_date:
        raise ValidationError("تاريخ التخصيص لا يجوز أن يسبق تاريخ سند القبض.")
    _check_allocation_target(batch, invoice, amount, allocation_date)
    allocation = CustomerReceiptBatchAllocation.objects.create(
        batch=batch, invoice=invoice, amount=amount, allocation_date=allocation_date, created_by=user,
    )
    AuditEvent.objects.create(
        actor=user, company=batch.company, branch=batch.branch,
        action=AuditEvent.Action.ACTION, resource="sales.customer_receipt_batch.allocated",
        object_id=str(batch.pk), http_method="POST", status_code=200, succeeded=True,
        metadata={
            "receipt_number": batch.receipt_number,
            "invoice_id": invoice.pk,
            "invoice_number": invoice.invoice_number,
            "amount": str(amount),
        },
    )
    return allocation


@transaction.atomic
def reverse_customer_receipt_batch(batch_id, user, entry_date):
    """عكس سند القبض المجمع بعد التأكد من عدم وجود تحصيلات أحدث تعتمد على تخصيصاته."""
    batch = CustomerReceiptBatch.objects.select_for_update(of=("self",)).select_related(
        "company", "customer", "branch", "journal_entry", "payment_account",
    ).get(pk=batch_id)
    if batch.status != CustomerReceiptBatch.Status.POSTED or not batch.journal_entry_id:
        raise ValidationError("لا يمكن عكس إلا سند قبض مجمع مرحّل.")
    if batch.journal_entry.status != JournalEntry.Status.POSTED:
        raise ValidationError("القيد المرتبط بسند القبض المجمع ليس مرحّلًا.")
    if entry_date < batch.receipt_date:
        raise ValidationError("تاريخ العكس لا يجوز أن يسبق تاريخ سند القبض.")
    active_refunds = CustomerRefund.objects.filter(
        source_receipt_batch=batch,
    ).filter(
        Q(status=CustomerRefund.Status.POSTED)
        | (
            Q(status=CustomerRefund.Status.REVERSED)
            & (
                Q(reversal_date__isnull=True)
                | Q(reversal_date__gte=entry_date)
                | Q(reversal_journal_entry__isnull=True)
                | ~Q(reversal_journal_entry__status=JournalEntry.Status.POSTED)
                | Q(reversal_treasury_transaction__isnull=True)
                | Q(reversal_treasury_transaction__journal_entry__isnull=True)
                | ~Q(reversal_treasury_transaction__status=TreasuryTransaction.Status.POSTED)
                | ~Q(reversal_treasury_transaction__journal_entry__status=JournalEntry.Status.POSTED)
            )
        )
    ).exists()
    if active_refunds:
        raise ValidationError("يجب عكس مردودات العميل المرتبطة بالدفعة أولًا قبل عكس سند القبض المجمع.")

    allocations = list(batch.allocations.select_related("invoice"))
    latest_allocation_date = max(
        (row.allocation_date for row in allocations),
        default=batch.receipt_date,
    )
    if entry_date < latest_allocation_date:
        raise ValidationError("تاريخ العكس لا يجوز أن يسبق آخر تاريخ تخصيص للدفعة.")
    if not allocations:
        later_direct = CustomerReceipt.objects.filter(
            customer=batch.customer, status=CustomerReceipt.Status.POSTED,
        ).filter(
            Q(receipt_date__gt=batch.receipt_date)
            | Q(receipt_date=batch.receipt_date, created_at__gt=batch.created_at)
        ).exists()
        later_batches = CustomerReceiptBatch.objects.filter(
            customer=batch.customer, status=CustomerReceiptBatch.Status.POSTED,
        ).exclude(pk=batch.pk).filter(
            Q(receipt_date__gt=batch.receipt_date)
            | Q(receipt_date=batch.receipt_date, created_at__gt=batch.created_at)
        ).exists()
        if later_direct or later_batches:
            raise ValidationError("يجب عكس أحدث دفعة مقدمة للعميل أولًا.")
    for allocation in allocations:
        later_direct = CustomerReceipt.objects.filter(
            invoice=allocation.invoice, status=CustomerReceipt.Status.POSTED,
        ).filter(
            Q(receipt_date__gt=allocation.allocation_date)
            | Q(receipt_date=allocation.allocation_date, created_at__gt=allocation.created_at)
        ).exists()
        later_batch = CustomerReceiptBatchAllocation.objects.filter(
            invoice=allocation.invoice, batch__status=CustomerReceiptBatch.Status.POSTED,
        ).exclude(batch=batch).filter(
            Q(allocation_date__gt=allocation.allocation_date)
            | Q(allocation_date=allocation.allocation_date, created_at__gt=allocation.created_at)
        ).exists()
        if later_direct or later_batch:
            raise ValidationError("يجب عكس أحدث سند قبض مخصص إلى الفاتورة أولًا.")

    cash_account_id = batch.payment_account_id or batch.journal_entry.lines.filter(
        description="استلام نقدية/تحويل من العميل",
    ).values_list("account_id", flat=True).first()
    cash_totals = JournalLine.objects.filter(
        account_id=cash_account_id,
        entry__entry_date__lte=entry_date,
        entry__status__in=(JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED),
    ).aggregate(debit=Sum("debit"), credit=Sum("credit"))
    opening_balance = TreasuryAccount.objects.filter(
        company_id=batch.company_id, account_id=cash_account_id,
        opening_journal_entry__isnull=True,
    ).aggregate(total=Sum("opening_balance"))["total"] or Decimal("0")
    cash_balance = opening_balance + (cash_totals["debit"] or Decimal("0")) - (cash_totals["credit"] or Decimal("0"))
    if cash_balance < batch.amount:
        raise ValidationError(
            f"لا يمكن عكس سند القبض: رصيد حساب الخزينة/البنك ({cash_balance}) أقل من مبلغ السند ({batch.amount})."
        )

    try:
        reversal = reverse_operational_journal(
            batch.journal_entry_id, user, entry_date, JournalEntry.Source.SALES,
        )
        from treasury.services import reverse_linked_document_treasury_movement
        reverse_linked_document_treasury_movement(
            journal_entry_id=batch.journal_entry_id,
            reversal_journal_entry=reversal,
            user=user,
        )
    except DjangoValidationError as exc:
        detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
        raise ValidationError(detail) from exc
    CustomerReceiptBatch.objects.filter(
        pk=batch.pk, status=CustomerReceiptBatch.Status.POSTED,
    ).update(
        status=CustomerReceiptBatch.Status.REVERSED,
        reversal_journal_entry=reversal,
        reversal_date=entry_date,
        reversed_at=timezone.now(),
        reversed_by=user,
    )
    AuditEvent.objects.create(
        actor=user, company=batch.company, branch=batch.branch,
        action=AuditEvent.Action.ACTION, resource="sales.customer_receipt_batch.reversed",
        object_id=str(batch.pk), http_method="POST", status_code=200, succeeded=True,
        metadata={
            "receipt_number": batch.receipt_number,
            "reversal_date": entry_date.isoformat(),
            "reversal_journal_entry_id": reversal.pk,
        },
    )
    batch.refresh_from_db()
    return batch
