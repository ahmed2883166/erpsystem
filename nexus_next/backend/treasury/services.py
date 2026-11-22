"""خدمات ترحيل حركات الخزينة والتحقق من الأرصدة والموافقات وربطها بالقيود."""

from django.core.exceptions import ValidationError, ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Sum
from decimal import Decimal
from django.utils import timezone

from accounting.models import JournalEntry, JournalLine
from companies.models import FiscalYear
from .models import TreasuryAccount, TreasuryTransaction


def record_posted_document_bank_movement(*, company, branch, fiscal_year, account, kind, amount,
                                         transaction_date, document_number, description,
                                         counter_account, journal_entry, user=None, reference=""):
    """تسجيل مرآة لحركة بنك مستند مرحّل وربطها بالقيد الأصلي دون إنشاء قيد مكرر."""
    expected_kind = TreasuryAccount.Kind.BANK if kind in (TreasuryTransaction.Kind.RECEIPT, TreasuryTransaction.Kind.PAYMENT) else None
    if expected_kind is None:
        raise ValidationError("نوع حركة البنك المرتبطة بالمستند غير مدعوم.")
    treasury_account = TreasuryAccount.objects.select_for_update().filter(
        company=company, account=account, kind=expected_kind, is_active=True,
    ).first()
    # The journal posting is authoritative. A missing or mismatched treasury mapping
    # must not roll back a valid sales/purchase receipt or payment; it only means
    # that this legacy/unmapped bank account cannot yet appear in bank reconciliation.
    if not treasury_account:
        return None
    if treasury_account.branch_id and treasury_account.branch_id != branch.pk:
        return None
    if treasury_account.currency_code != company.base_currency.code:
        return None
    if not journal_entry or journal_entry.status != JournalEntry.Status.POSTED:
        raise ValidationError("لا يمكن ربط حركة البنك قبل ترحيل القيد الأصلي.")
    return TreasuryTransaction.objects.create(
        company=company, branch=branch, fiscal_year=fiscal_year,
        source_account=treasury_account, counter_account=counter_account,
        kind=kind, status=TreasuryTransaction.Status.POSTED,
        document_number=document_number, transaction_date=transaction_date,
        amount=amount, description=description[:500], reference=reference,
        journal_entry=journal_entry, created_by=user, posted_at=timezone.now(),
    )


@transaction.atomic
def post_treasury_transaction(transaction_id, user=None):
    """ترحيل حركة الخزينة إلى القيد المحاسبي وربطها بالحساب والفرع."""
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
    # لا توجد بعد آلية سعر صرف/إعادة تقييم للعملات الأجنبية؛ ترحيل القيمة الاسمية
    # مباشرة إلى دفتر العملة الأساسية سيُنتج قيدًا محاسبيًا مضللًا.
    base_currency = doc.company.base_currency.code
    if source.currency_code != base_currency:
        raise ValidationError(
            f"لا يمكن ترحيل حركة على حساب بعملة {source.currency_code} قبل تفعيل سعر الصرف وفروق العملة؛ "
            f"العملة الأساسية للشركة هي {base_currency}."
        )
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
        if destination.currency_code != base_currency:
            raise ValidationError(
                f"لا يمكن ترحيل تحويل إلى حساب بعملة {destination.currency_code} قبل تفعيل سعر الصرف وفروق العملة."
            )
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
