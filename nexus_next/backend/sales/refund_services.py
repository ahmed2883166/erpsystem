"""خدمات رد مبالغ العملاء من مردودات المبيعات والدفعات المقدمة."""

from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from accounting.models import AccountRole, FiscalYear
from accounting.services import get_account_for_role
from audit.models import AuditEvent
from companies.access import resolve_warehouse_branch
from sales.models import CustomerRefund, CustomerReceiptBatch, SalesReturn
from treasury.models import TreasuryAccount, TreasuryTransaction
from treasury.services import post_treasury_transaction


def _refund_error(exc):
    detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
    raise ValidationError(detail) from exc


@transaction.atomic
def post_customer_refund(refund_id, user):
    """صرف رد العميل من الخزينة/البنك مع تخفيض رصيد العميل بالقيد المقابل."""
    refund = CustomerRefund.objects.select_for_update(of=("self",)).select_related(
        "company__base_currency", "customer", "branch", "treasury_account",
    ).get(pk=refund_id)
    if refund.status != CustomerRefund.Status.DRAFT:
        raise ValidationError("مستند رد مبلغ العميل ليس مسودة.")

    if not refund.amount.is_finite() or refund.amount <= 0:
        raise ValidationError("أدخل مبلغ رد صحيحًا وأكبر من صفر.")
    if refund.amount >= Decimal("10000000000000000") or refund.amount != refund.amount.quantize(Decimal("0.0001")):
        raise ValidationError("مبلغ الرد يجب ألا يتجاوز 16 رقمًا صحيحًا و4 منازل عشرية.")

    if refund.source_return_id:
        source = SalesReturn.objects.select_for_update(of=("self",)).select_related(
            "invoice__delivery__warehouse", "invoice__branch", "branch", "company",
        ).get(pk=refund.source_return_id)
        if source.status != SalesReturn.Status.POSTED or source.reversal_journal_entry_id:
            raise ValidationError("لا يمكن رد مبلغ إلا عن مردود مبيعات مرحّل وغير معكوس.")
        source_date = source.return_date
        source_branch_id = source.branch_id or source.invoice.branch_id
        if source_branch_id is None and source.invoice.delivery_id:
            source_branch_id = resolve_warehouse_branch(
                source.company, source.invoice.delivery.warehouse,
            ).pk
        posted_refunds = source.refunds.filter(
            status=CustomerRefund.Status.POSTED,
        ).exclude(pk=refund.pk)
        latest_refund_date = posted_refunds.order_by("-refund_date").values_list("refund_date", flat=True).first()
        if latest_refund_date and refund.refund_date < latest_refund_date:
            raise ValidationError("تاريخ رد المبلغ لا يجوز أن يسبق آخر رد سابق عن نفس المردود.")
        already_refunded = posted_refunds.aggregate(total=Sum("amount"))["total"] or Decimal("0")
        available = max(source.grand_total - already_refunded, Decimal("0"))
        source_label = f"مردود المبيعات {source.return_number}"
    else:
        source = CustomerReceiptBatch.objects.select_for_update(of=("self",)).select_related(
            "company", "customer", "branch",
        ).get(pk=refund.source_receipt_batch_id)
        if source.status != CustomerReceiptBatch.Status.POSTED:
            raise ValidationError("لا يمكن رد دفعة مقدمة إلا من سند قبض مجمع مرحّل.")
        source_date = source.receipt_date
        source_branch_id = source.branch_id
        latest_allocation = source.allocations.order_by("-allocation_date", "-id").values_list(
            "allocation_date", flat=True,
        ).first()
        if latest_allocation and refund.refund_date < latest_allocation:
            raise ValidationError("تاريخ رد الدفعة المقدمة لا يجوز أن يسبق آخر تخصيص لها.")
        latest_refund_date = source.refunds.filter(
            status=CustomerRefund.Status.POSTED,
        ).exclude(pk=refund.pk).order_by("-refund_date").values_list("refund_date", flat=True).first()
        if latest_refund_date and refund.refund_date < latest_refund_date:
            raise ValidationError("تاريخ رد الدفعة المقدمة لا يجوز أن يسبق آخر رد سابق لها.")
        available = source.unallocated_amount + (
            refund.amount if refund.pk and refund.status == CustomerRefund.Status.POSTED else Decimal("0")
        )
        source_label = f"الدفعة المقدمة {source.receipt_number}"

    if refund.refund_date < source_date:
        raise ValidationError("تاريخ رد المبلغ لا يجوز أن يسبق تاريخ المستند الأصلي.")
    if source_branch_id != refund.branch_id:
        raise ValidationError("فرع رد المبلغ يجب أن يطابق فرع المستند الأصلي.")
    if refund.amount > available:
        raise ValidationError(f"مبلغ الرد يتجاوز الرصيد المتاح للمستند ({available}).")

    account = TreasuryAccount.objects.select_for_update(of=("self",)).select_related(
        "company__base_currency", "branch", "account",
    ).get(pk=refund.treasury_account_id)
    if account.company_id != refund.company_id or not account.is_active:
        raise ValidationError("اختر حساب خزينة/بنك نشطًا تابعًا للشركة.")
    if account.currency_code != refund.company.base_currency.code:
        raise ValidationError("رد المبلغ متاح حاليًا من حساب خزينة/بنك بالعملة الأساسية للشركة.")
    if account.branch_id and account.branch_id != refund.branch_id:
        raise ValidationError("حساب الخزينة لا يتبع فرع رد المبلغ.")
    if account.kind not in (TreasuryAccount.Kind.CASH, TreasuryAccount.Kind.BANK):
        raise ValidationError("اختر خزينة نقدية أو حسابًا بنكيًا.")

    year = FiscalYear.objects.filter(
        company=refund.company, status=FiscalYear.Status.OPEN,
        start_date__lte=refund.refund_date, end_date__gte=refund.refund_date,
    ).order_by("-start_date").first()
    if not year:
        raise ValidationError("لا توجد سنة مالية مفتوحة تشمل تاريخ رد المبلغ.")

    receivable = get_account_for_role(refund.company, AccountRole.Role.ACCOUNTS_RECEIVABLE)
    if receivable.company_id != refund.company_id:
        raise ValidationError("حساب العملاء المرتبط بالشركة غير صالح.")

    treasury_doc = TreasuryTransaction.objects.create(
        company=refund.company,
        branch=refund.branch,
        fiscal_year=year,
        source_account=account,
        counter_account=receivable,
        kind=TreasuryTransaction.Kind.PAYMENT,
        document_number=f"SALE-REF-{refund.pk}",
        transaction_date=refund.refund_date,
        amount=refund.amount,
        description=f"رد مبلغ للعميل {refund.customer.name_ar} مقابل {source_label}"[:500],
        reference=refund.reference,
        created_by=user,
    )
    try:
        treasury_doc = post_treasury_transaction(treasury_doc.pk, user)
    except DjangoValidationError as exc:
        _refund_error(exc)

    refund.treasury_transaction = treasury_doc
    refund.status = CustomerRefund.Status.POSTED
    refund.posted_at = timezone.now()
    refund.save(update_fields=("treasury_transaction", "status", "posted_at"))
    AuditEvent.objects.create(
        actor=user, company=refund.company, branch=refund.branch,
        action=AuditEvent.Action.ACTION, resource="sales.customer_refund.posted",
        object_id=str(refund.pk), http_method="POST", status_code=200, succeeded=True,
        metadata={
            "refund_number": refund.refund_number,
            "amount": str(refund.amount),
            "source_return_id": refund.source_return_id,
            "source_receipt_batch_id": refund.source_receipt_batch_id,
            "treasury_transaction_id": treasury_doc.pk,
            "journal_entry_id": treasury_doc.journal_entry_id,
        },
    )
    return refund


@transaction.atomic
def reverse_customer_refund(refund_id, user, entry_date):
    """إلغاء رد مبلغ بقبض مقابل من نفس الخزينة وإعادة رصيد العميل."""
    refund = CustomerRefund.objects.select_for_update(of=("self",)).select_related(
        "company", "customer", "branch", "treasury_account",
        "treasury_transaction__fiscal_year", "treasury_transaction__journal_entry",
    ).get(pk=refund_id)
    if refund.status != CustomerRefund.Status.POSTED or not refund.treasury_transaction_id:
        raise ValidationError("لا يمكن عكس إلا رد مبلغ عميل مرحّل.")
    if refund.reversal_treasury_transaction_id:
        raise ValidationError("تم عكس رد المبلغ من قبل.")
    if entry_date < refund.refund_date:
        raise ValidationError("تاريخ العكس لا يجوز أن يسبق تاريخ رد المبلغ.")
    if refund.source_return_id:
        later_refunds = CustomerRefund.objects.filter(
            source_return_id=refund.source_return_id,
            status=CustomerRefund.Status.POSTED,
            refund_date__gt=refund.refund_date,
        ).exclude(pk=refund.pk)
    else:
        later_refunds = CustomerRefund.objects.filter(
            source_receipt_batch_id=refund.source_receipt_batch_id,
            status=CustomerRefund.Status.POSTED,
            refund_date__gt=refund.refund_date,
        ).exclude(pk=refund.pk)
    if later_refunds.exists():
        raise ValidationError("يجب عكس أحدث رد مبلغ عن نفس المستند أولًا.")

    original = refund.treasury_transaction
    if not original.journal_entry_id or original.journal_entry.status != "posted":
        raise ValidationError("قيد رد المبلغ الأصلي ليس مرحّلًا.")

    year = FiscalYear.objects.filter(
        company=refund.company, status=FiscalYear.Status.OPEN,
        start_date__lte=entry_date, end_date__gte=entry_date,
    ).order_by("-start_date").first()
    if not year:
        raise ValidationError("لا توجد سنة مالية مفتوحة تشمل تاريخ عكس رد المبلغ.")
    receivable = get_account_for_role(refund.company, AccountRole.Role.ACCOUNTS_RECEIVABLE)
    reversal = TreasuryTransaction.objects.create(
        company=refund.company,
        branch=refund.branch,
        fiscal_year=year,
        source_account=refund.treasury_account,
        counter_account=receivable,
        kind=TreasuryTransaction.Kind.RECEIPT,
        document_number=f"SALE-REF-REV-{refund.pk}",
        transaction_date=entry_date,
        amount=refund.amount,
        description=f"عكس رد مبلغ العميل {refund.customer.name_ar} رقم {refund.refund_number}"[:500],
        reference=refund.reference,
        created_by=user,
    )
    try:
        reversal = post_treasury_transaction(reversal.pk, user)
    except DjangoValidationError as exc:
        _refund_error(exc)

    refund.reversal_treasury_transaction = reversal
    refund.reversal_journal_entry = reversal.journal_entry
    refund.reversal_date = entry_date
    refund.reversed_at = timezone.now()
    refund.reversed_by = user
    refund.status = CustomerRefund.Status.REVERSED
    refund.save(update_fields=(
        "reversal_treasury_transaction", "reversal_journal_entry",
        "reversal_date", "reversed_at", "reversed_by", "status",
    ))
    AuditEvent.objects.create(
        actor=user, company=refund.company, branch=refund.branch,
        action=AuditEvent.Action.ACTION, resource="sales.customer_refund.reversed",
        object_id=str(refund.pk), http_method="POST", status_code=200, succeeded=True,
        metadata={
            "refund_number": refund.refund_number,
            "reversal_date": entry_date.isoformat(),
            "reversal_treasury_transaction_id": reversal.pk,
            "reversal_journal_entry_id": reversal.journal_entry_id,
        },
    )
    return refund
