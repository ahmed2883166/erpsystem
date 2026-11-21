from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from accounting.models import JournalEntry, JournalLine
from companies.models import FiscalYear
from .models import TreasuryTransaction


@transaction.atomic
def post_treasury_transaction(transaction_id, user=None):
    doc = TreasuryTransaction.objects.select_for_update().select_related(
        "company", "branch", "fiscal_year", "source_account__account",
        "destination_account__account", "counter_account",
    ).get(pk=transaction_id)
    if doc.status != TreasuryTransaction.Status.DRAFT:
        raise ValidationError("لا يمكن ترحيل إلا مستند خزينة مسودة.")
    if doc.fiscal_year.status != FiscalYear.Status.OPEN:
        raise ValidationError("السنة المالية مغلقة.")
    if not (doc.fiscal_year.start_date <= doc.transaction_date <= doc.fiscal_year.end_date):
        raise ValidationError("تاريخ المستند خارج السنة المالية.")
    source = doc.source_account
    if not source.is_active:
        raise ValidationError("حساب الخزينة المصدر موقوف.")
    if source.branch_id and source.branch_id != doc.branch_id:
        raise ValidationError("فرع المستند لا يطابق فرع الخزينة.")
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
        if destination.branch_id and destination.branch_id != doc.branch_id:
            raise ValidationError("التحويل بين الفروع يحتاج معالجة تسوية بين الفروع؛ لا يمكن ترحيله كتحويل داخلي.")
        if destination.currency_code != source.currency_code:
            raise ValidationError("التحويل بين عملتين مختلفتين غير مدعوم قبل إضافة فروق العملة.")
        debit_account, credit_account = destination.account, source.account

    entry = JournalEntry.objects.create(
        company=doc.company, branch=doc.branch, fiscal_year=doc.fiscal_year,
        entry_number=f"TR-{doc.pk}", entry_date=doc.transaction_date,
        description=doc.description, source_type=JournalEntry.Source.MANUAL,
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
    return doc
