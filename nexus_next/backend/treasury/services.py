from django.core.exceptions import ValidationError, ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Sum
from decimal import Decimal
from django.utils import timezone

from accounting.models import JournalEntry, JournalLine
from companies.models import FiscalYear
from .models import TreasuryAccount, TreasuryTransaction


@transaction.atomic
def post_treasury_transaction(transaction_id, user=None):
    doc = TreasuryTransaction.objects.select_for_update(of=("self",)).select_related(
        "company", "branch", "fiscal_year", "source_account__account",
        "destination_account__account", "counter_account",
    ).get(pk=transaction_id)
    if doc.status != TreasuryTransaction.Status.DRAFT:
        raise ValidationError("لا يمكن ترحيل إلا مستند خزينة مسودة.")
    from approvals.models import FinancialApprovalRequest
    from approvals.services import require_financial_approval
    try:
        require_financial_approval(FinancialApprovalRequest.DocumentType.TREASURY_PAYMENT, doc)
    except DjangoValidationError as exc:
        raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages) from exc
    if doc.fiscal_year.status != FiscalYear.Status.OPEN:
        raise ValidationError("السنة المالية مغلقة.")
    if not (doc.fiscal_year.start_date <= doc.transaction_date <= doc.fiscal_year.end_date):
        raise ValidationError("تاريخ المستند خارج السنة المالية.")
    # Serialize balance checks per treasury account so concurrent payments cannot both spend the same balance.
    account_ids = {doc.source_account_id}
    if doc.destination_account_id:
        account_ids.add(doc.destination_account_id)
    locked_accounts = {
        account.pk: account
        for account in TreasuryAccount.objects.select_for_update(of=("self",))
        .filter(pk__in=sorted(account_ids)).order_by("pk")
    }
    source = locked_accounts[doc.source_account_id]
    if doc.destination_account_id:
        doc.destination_account = locked_accounts[doc.destination_account_id]
    doc.source_account = source
    if not source.is_active:
        raise ValidationError("حساب الخزينة المصدر موقوف.")
    if source.account.nature != source.account.Nature.ASSET:
        raise ValidationError("يجب ربط الخزينة بحساب من طبيعة الأصول.")
    if source.branch_id and source.branch_id != doc.branch_id:
        raise ValidationError("فرع المستند لا يطابق فرع الخزينة.")
    totals = source.account.journal_lines.filter(entry__status=JournalEntry.Status.POSTED).aggregate(debit=Sum("debit"), credit=Sum("credit"))
    opening_base = Decimal("0") if source.opening_journal_entry_id else source.opening_balance
    available = opening_base + (totals["debit"] or Decimal("0")) - (totals["credit"] or Decimal("0"))
    if doc.kind in (TreasuryTransaction.Kind.PAYMENT, TreasuryTransaction.Kind.TRANSFER) and available < doc.amount:
        raise ValidationError("الرصيد المتاح غير كافٍ لتنفيذ الحركة؛ تم إيقاف الترحيل لمنع الرصيد السالب.")
    debit_account = credit_account = None
    if doc.kind == TreasuryTransaction.Kind.RECEIPT:
        if not doc.counter_account.is_active or doc.counter_account.is_group or not doc.counter_account.allow_posting:
            raise ValidationError("الحساب المقابل غير صالح للترحيل.")
        debit_account, credit_account = source.account, doc.counter_account
    elif doc.kind == TreasuryTransaction.Kind.PAYMENT:
        if not doc.counter_account.is_active or doc.counter_account.is_group or not doc.counter_account.allow_posting:
            raise ValidationError("الحساب المقابل غير صالح للترحيل.")
        debit_account, credit_account = doc.counter_account, source.account
    else:
        destination = doc.destination_account
        if not destination.is_active:
            raise ValidationError("حساب الخزينة المستقبلة موقوف.")
        if destination.account.nature != destination.account.Nature.ASSET:
            raise ValidationError("يجب ربط الخزينة المستقبلة بحساب من طبيعة الأصول.")
        if destination.branch_id and destination.branch_id != doc.branch_id:
            raise ValidationError("التحويل بين الفروع يحتاج معالجة تسوية بين الفروع؛ لا يمكن ترحيله كتحويل داخلي.")
        if destination.currency_code != source.currency_code:
            raise ValidationError("التحويل بين عملتين مختلفتين غير مدعوم قبل إضافة فروق العملة.")
        debit_account, credit_account = destination.account, source.account

    from assets.models import FixedAsset
    asset_acquisition = FixedAsset.objects.filter(acquisition_treasury_transaction_id=doc.pk).first()
    entry = JournalEntry.objects.create(
        company=doc.company, branch=doc.branch, fiscal_year=doc.fiscal_year,
        entry_number=f"FA-ACQ-{asset_acquisition.pk}" if asset_acquisition else f"TR-{doc.pk}",
        entry_date=doc.transaction_date,
        description=doc.description,
        source_type=JournalEntry.Source.ASSETS if asset_acquisition else JournalEntry.Source.MANUAL,
        created_by=user or doc.created_by,
    )
    JournalLine.objects.bulk_create([
        JournalLine(entry=entry, account=debit_account, debit=doc.amount, credit=0, description=doc.description),
        JournalLine(entry=entry, account=credit_account, debit=0, credit=doc.amount, description=doc.description),
    ])
    from accounting.services import post_journal
    post_journal(entry.pk)
    doc.status = TreasuryTransaction.Status.POSTED
    doc.journal_entry = entry
    doc.posted_at = timezone.now()
    doc.save(update_fields=("status", "journal_entry", "posted_at"))
    if asset_acquisition and not asset_acquisition.acquisition_journal_id:
        asset_acquisition.acquisition_journal = entry
        asset_acquisition.save(update_fields=("acquisition_journal",))
    return doc
