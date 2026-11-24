"""خدمات ترحيل الخزينة ومراكز العملات الأجنبية وإعادة تقييمها."""

from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError, ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from accounting.models import JournalEntry, JournalLine
from companies.models import ExchangeRate, FiscalYear
from .models import (
    TreasuryAccount, TreasuryTransaction, TreasuryFxPosition,
    TreasuryFxRevaluation,
)


MONEY = Decimal("0.0001")


def _money(value):
    return Decimal(value).quantize(MONEY, rounding=ROUND_HALF_UP)


def _foreign_position(account):
    """قفل أو إنشاء مركز عملة، مع منع ترحيل أرصدة افتتاحية غير مُمثلة اسميًا."""
    if account.opening_balance != 0 or account.opening_journal_entry_id:
        raise ValidationError(
            "لا يمكن ترحيل حساب أجنبي له رصيد افتتاحي قديم قبل إدخال رصيد افتتاحي بالعملة الأجنبية وسعره المعتمد."
        )
    position, created = TreasuryFxPosition.objects.select_for_update().get_or_create(
        account=account,
        defaults={"foreign_amount": Decimal("0"), "carrying_base_amount": Decimal("0")},
    )
    if created and TreasuryTransaction.objects.filter(
        Q(source_account=account) | Q(destination_account=account),
        status=TreasuryTransaction.Status.POSTED,
    ).exists():
        raise ValidationError(
            "للحساب حركات أجنبية مرحّلة سابقة دون سجل مركز عملة؛ يلزم تسوية افتتاحية موثقة قبل المتابعة."
        )
    if position.foreign_amount < 0 or position.carrying_base_amount < 0:
        raise ValidationError("مركز العملة يحتوي على رصيد غير صالح.")
    if (position.foreign_amount == 0) != (position.carrying_base_amount == 0):
        raise ValidationError("مركز العملة غير متوازن؛ أوقف الترحيل وراجع سجل التسويات.")
    return position


def record_posted_document_bank_movement(*, company, branch, fiscal_year, account, kind, amount,
                                         transaction_date, document_number, description,
                                         counter_account, journal_entry, user=None, reference="", cash_flow_category=None):
    """تسجيل مرآة لحركة بنك مستند مرحّل وربطها بالقيد الأصلي دون إنشاء قيد مكرر."""
    expected_kind = TreasuryAccount.Kind.BANK if kind in (
        TreasuryTransaction.Kind.RECEIPT, TreasuryTransaction.Kind.PAYMENT
    ) else None
    if expected_kind is None:
        raise ValidationError("نوع حركة البنك المرتبطة بالمستند غير مدعوم.")
    treasury_account = TreasuryAccount.objects.select_for_update().filter(
        company=company, account=account, kind=expected_kind, is_active=True,
    ).first()
    if not treasury_account:
        return None
    if treasury_account.branch_id and treasury_account.branch_id != branch.pk:
        return None
    # لا ننشئ مرآة أجنبية من مستندات لا تحمل لقطة عملة كاملة.
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
        **({"cash_flow_category": cash_flow_category} if cash_flow_category else {}),
    )


@transaction.atomic
def post_treasury_transaction(transaction_id, user=None):
    """ترحيل حركة خزينة مع احتساب متوسط التكلفة وفروق العملة المحققة."""
    doc = TreasuryTransaction.objects.select_for_update(of=("self",)).select_related(
        "company__base_currency", "branch", "fiscal_year", "source_account__account",
        "destination_account__account", "counter_account", "transaction_currency",
    ).get(pk=transaction_id)
    if doc.status != TreasuryTransaction.Status.DRAFT:
        raise ValidationError("لا يمكن ترحيل إلا مستند خزينة مسودة.")
    from approvals.models import FinancialApprovalRequest
    from approvals.services import require_financial_approval
    try:
        require_financial_approval(FinancialApprovalRequest.DocumentType.TREASURY_PAYMENT, doc)
    except DjangoValidationError as exc:
        if getattr(exc, "code", None) == "approval_required":
            raise ValidationError(str(exc), code="approval_required") from exc
        raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages) from exc
    if doc.fiscal_year.status != FiscalYear.Status.OPEN:
        raise ValidationError("السنة المالية مغلقة.")
    if not (doc.fiscal_year.start_date <= doc.transaction_date <= doc.fiscal_year.end_date):
        raise ValidationError("تاريخ المستند خارج السنة المالية.")

    account_ids = {doc.source_account_id}
    if doc.destination_account_id:
        account_ids.add(doc.destination_account_id)
    locked_accounts = {
        account.pk: account
        for account in TreasuryAccount.objects.select_for_update(of=("self",))
        .select_related("company__base_currency", "account")
        .filter(pk__in=sorted(account_ids)).order_by("pk")
    }
    source = locked_accounts[doc.source_account_id]
    destination = locked_accounts.get(doc.destination_account_id)
    doc.source_account = source
    if destination:
        doc.destination_account = destination
    if not source.is_active:
        raise ValidationError("حساب الخزينة المصدر موقوف.")
    if source.account.nature != source.account.Nature.ASSET:
        raise ValidationError("يجب ربط الخزينة بحساب من طبيعة الأصول.")
    if source.branch_id and source.branch_id != doc.branch_id:
        raise ValidationError("فرع المستند لا يطابق فرع الخزينة.")

    base_currency = doc.company.base_currency.code
    foreign = source.currency_code != base_currency
    if foreign:
        # لا نقبل حركة مؤرخة في أو قبل آخر إعادة تقييم؛ وإلا سيتغير المركز
        # التاريخي بعد إقفال التقييم وتصبح القيمة الدفترية اللاحقة غير موثوقة.
        last_revaluation = TreasuryFxRevaluation.objects.filter(
            account_id__in=account_ids, revaluation_date__gte=doc.transaction_date,
        ).order_by("-revaluation_date").first()
        if last_revaluation:
            raise ValidationError("تاريخ الحركة يسبق أو يساوي آخر إعادة تقييم؛ استخدم تاريخًا لاحقًا أو اعكس التقييم بإجراء معتمد.")
        if not all((doc.transaction_currency_id, doc.transaction_amount, doc.exchange_rate, doc.exchange_rate_date)):
            raise ValidationError("الحركة الأجنبية تتطلب لقطة مبلغ وسعر صرف وتاريخ معتمد مكتملة.")
        if doc.transaction_currency.code != source.currency_code:
            raise ValidationError("عملة الحركة لا تطابق عملة حساب الخزينة.")
        if doc.amount != _money(doc.transaction_amount * doc.exchange_rate):
            raise ValidationError("القيمة الأساسية لا تطابق المبلغ الأجنبي مضروبًا في سعر الصرف.")
        source_position = _foreign_position(source)
        if source_position.foreign_amount < 0:
            raise ValidationError("رصيد العملة الأجنبية غير صالح.")
        carrying_amount = Decimal("0")
        fx_difference = Decimal("0")
        if doc.kind in (TreasuryTransaction.Kind.PAYMENT, TreasuryTransaction.Kind.TRANSFER):
            if source_position.foreign_amount < doc.transaction_amount:
                raise ValidationError("رصيد العملة الأجنبية المتاح غير كافٍ؛ تم إيقاف الترحيل لمنع الرصيد السالب.")
            if source_position.foreign_amount == doc.transaction_amount:
                carrying_amount = source_position.carrying_base_amount
            else:
                carrying_amount = _money(
                    source_position.carrying_base_amount
                    * doc.transaction_amount / source_position.foreign_amount
                )
            fx_difference = _money(doc.amount - carrying_amount)
        else:
            carrying_amount = doc.amount
        doc.carrying_amount = carrying_amount
    else:
        source_position = None
        carrying_amount = doc.amount
        fx_difference = Decimal("0")
        doc.carrying_amount = None
        totals = source.account.journal_lines.filter(
            entry__status__in=(JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED)
        ).aggregate(debit=Sum("debit"), credit=Sum("credit"))
        opening_base = Decimal("0") if source.opening_journal_entry_id else source.opening_balance
        available = opening_base + (totals["debit"] or Decimal("0")) - (totals["credit"] or Decimal("0"))
        if doc.kind in (TreasuryTransaction.Kind.PAYMENT, TreasuryTransaction.Kind.TRANSFER) and available < doc.amount:
            raise ValidationError("الرصيد المتاح غير كافٍ لتنفيذ الحركة؛ تم إيقاف الترحيل لمنع الرصيد السالب.")

    from assets.models import FixedAsset
    asset_acquisition = FixedAsset.objects.filter(
        acquisition_treasury_transaction_id=doc.pk,
    ).select_related("company", "asset_account").first()

    debit_lines = []
    credit_lines = []
    description = doc.description
    if doc.kind == TreasuryTransaction.Kind.RECEIPT:
        if not doc.counter_account.is_active or doc.counter_account.is_group or not doc.counter_account.allow_posting:
            raise ValidationError("الحساب المقابل غير صالح للترحيل.")
        debit_lines.append((source.account, doc.amount if not foreign else carrying_amount))
        credit_lines.append((doc.counter_account, doc.amount))
        if foreign:
            source_position.foreign_amount += doc.transaction_amount
            source_position.carrying_base_amount += carrying_amount

    elif doc.kind == TreasuryTransaction.Kind.PAYMENT:
        if not doc.counter_account.is_active or doc.counter_account.is_group or not doc.counter_account.allow_posting:
            raise ValidationError("الحساب المقابل غير صالح للترحيل.")
        if asset_acquisition and asset_acquisition.recoverable_tax_amount:
            from accounting.services import get_account_for_role
            recoverable_tax = asset_acquisition.recoverable_tax_amount
            if (
                doc.counter_account_id != asset_acquisition.asset_account_id
                or doc.amount != asset_acquisition.original_cost + recoverable_tax
            ):
                raise ValidationError("قيمة حركة خزينة اقتناء الأصل لا تطابق تكلفة الأصل وضريبة المدخلات.")
            vat_input = get_account_for_role(asset_acquisition.company, "vat_input")
            if (
                vat_input.pk in (doc.counter_account_id, source.account_id)
                or vat_input.nature != "asset" or not vat_input.is_active
                or vat_input.is_group or not vat_input.allow_posting
            ):
                raise ValidationError("حساب ضريبة المدخلات غير صالح أو غير مستقل عن حساب الأصل والبنك.")
            debit_lines.append((doc.counter_account, asset_acquisition.original_cost))
            debit_lines.append((vat_input, recoverable_tax))
        else:
            debit_lines.append((doc.counter_account, doc.amount))
        credit_lines.append((source.account, carrying_amount))
        if foreign:
            if fx_difference:
                from accounting.services import get_account_for_role
                if fx_difference > 0:
                    credit_lines.append((get_account_for_role(doc.company, "realized_fx_gain"), fx_difference))
                else:
                    debit_lines.append((get_account_for_role(doc.company, "realized_fx_loss"), -fx_difference))
            # تحديث المركز الاسمي والدفترى واجب حتى عندما لا يوجد فرق عملة محقق.
            source_position.foreign_amount -= doc.transaction_amount
            source_position.carrying_base_amount -= carrying_amount

    else:
        if not destination or not destination.is_active:
            raise ValidationError("حساب الخزينة المستقبلة غير متاح.")
        if destination.account.nature != destination.account.Nature.ASSET:
            raise ValidationError("يجب ربط الخزينة المستقبلة بحساب من طبيعة الأصول.")
        if destination.branch_id and destination.branch_id != doc.branch_id:
            raise ValidationError("التحويل بين الفروع يحتاج معالجة تسوية بين الفروع.")
        if destination.currency_code != source.currency_code:
            raise ValidationError("التحويل بين عملتين مختلفتين غير مدعوم.")
        debit_lines.append((destination.account, carrying_amount))
        credit_lines.append((source.account, carrying_amount))
        if foreign:
            destination_position = _foreign_position(destination)
            source_position.foreign_amount -= doc.transaction_amount
            source_position.carrying_base_amount -= carrying_amount
            destination_position.foreign_amount += doc.transaction_amount
            destination_position.carrying_base_amount += carrying_amount
            destination_position.save(update_fields=("foreign_amount", "carrying_base_amount", "updated_at"))

    if foreign:
        source_position.save(update_fields=("foreign_amount", "carrying_base_amount", "updated_at"))

    entry = JournalEntry.objects.create(
        company=doc.company, branch=doc.branch, fiscal_year=doc.fiscal_year,
        entry_number=f"FA-ACQ-{asset_acquisition.pk}" if asset_acquisition else f"TR-{doc.pk}",
        entry_date=doc.transaction_date, description=description,
        source_type=JournalEntry.Source.ASSETS if asset_acquisition else JournalEntry.Source.MANUAL,
        created_by=user or doc.created_by,
    )
    fx_snapshot = {
        "transaction_currency": doc.transaction_currency,
        "transaction_amount": doc.transaction_amount,
        "exchange_rate": doc.exchange_rate,
        "exchange_rate_date": doc.exchange_rate_date,
    } if foreign else {}
    JournalLine.objects.bulk_create([
        *[
            JournalLine(
                entry=entry, account=account, debit=amount, credit=0,
                description=description,
                **(
                    fx_snapshot
                    if foreign and (
                        doc.kind == TreasuryTransaction.Kind.RECEIPT
                        or (doc.kind == TreasuryTransaction.Kind.PAYMENT and account.pk == doc.counter_account_id)
                    )
                    else {}
                ),
            )
            for account, amount in debit_lines if amount > 0
        ],
        *[
            JournalLine(
                entry=entry, account=account, debit=0, credit=amount,
                description=description,
                **(fx_snapshot if foreign and doc.kind == TreasuryTransaction.Kind.RECEIPT else {}),
            )
            for account, amount in credit_lines if amount > 0
        ],
    ])
    from accounting.services import post_journal
    post_journal(entry.pk, actor=user or doc.created_by)
    doc.status = TreasuryTransaction.Status.POSTED
    doc.journal_entry = entry
    doc.posted_at = timezone.now()
    doc.save(update_fields=("status", "journal_entry", "posted_at", "carrying_amount"))
    if asset_acquisition and not asset_acquisition.acquisition_journal_id:
        asset_acquisition.acquisition_journal = entry
        asset_acquisition.save(update_fields=("acquisition_journal",))
    return doc


@transaction.atomic
def revalue_treasury_fx_account(*, account_id, fiscal_year_id, revaluation_date,
                                rate_date, branch, user=None):
    """إعادة تقييم رصيد خزينة أجنبي بسعر معتمد وإنشاء قيد فرق غير محقق."""
    account = TreasuryAccount.objects.select_for_update(of=("self",)).select_related(
        "company__base_currency", "account", "branch",
    ).get(pk=account_id)
    if not account.is_active:
        raise ValidationError("لا يمكن إعادة تقييم حساب خزينة موقوف.")
    if account.currency_code == account.company.base_currency.code:
        raise ValidationError("إعادة التقييم متاحة لحسابات العملات الأجنبية فقط.")
    if account.account.nature != account.account.Nature.ASSET:
        raise ValidationError("حساب الخزينة يجب أن يكون من طبيعة الأصول.")
    if account.opening_balance != 0 or account.opening_journal_entry_id:
        raise ValidationError("الحساب له رصيد افتتاحي غير ممثل بعملة أجنبية؛ عالج الرصيد الافتتاحي أولًا.")
    if account.branch_id and account.branch_id != branch.pk:
        raise ValidationError("الفرع المحدد لا يطابق فرع حساب الخزينة.")
    if branch.company_id != account.company_id:
        raise ValidationError("الفرع لا يتبع الشركة.")
    year = FiscalYear.objects.select_for_update().get(pk=fiscal_year_id, company=account.company)
    if year.status != FiscalYear.Status.OPEN:
        raise ValidationError("السنة المالية مغلقة.")
    if not (year.start_date <= revaluation_date <= year.end_date):
        raise ValidationError("تاريخ إعادة التقييم خارج السنة المالية.")
    # لا يجوز تقييم رصيد حالي بتاريخ أقدم من حركة خزينة مرحّلة؛ المركز الحالي
    # يتضمن هذه الحركة وسيؤدي ذلك إلى إعادة تقييم تاريخية غير صحيحة.
    later_movements = TreasuryTransaction.objects.filter(
        Q(source_account=account) | Q(destination_account=account),
        status=TreasuryTransaction.Status.POSTED,
        transaction_date__gt=revaluation_date,
    )
    if later_movements.exists():
        raise ValidationError("يوجد حركة خزينة مرحّلة بتاريخ لاحق؛ أعد التقييم بعد آخر حركة أو عالج التسلسل الزمني أولًا.")
    if rate_date > revaluation_date:
        raise ValidationError("تاريخ سعر الصرف لا يجوز أن يتجاوز تاريخ إعادة التقييم.")
    if TreasuryFxRevaluation.objects.filter(account=account, revaluation_date=revaluation_date).exists():
        raise ValidationError("تمت إعادة تقييم هذا الحساب في التاريخ نفسه من قبل.")
    rate_row = ExchangeRate.objects.filter(
        company=account.company, currency__code=account.currency_code,
        rate_date=rate_date,
    ).first()
    if not rate_row:
        raise ValidationError("لا يوجد سعر صرف معتمد لهذه العملة في التاريخ المحدد.")

    position = _foreign_position(account)
    previous = position.carrying_base_amount
    new_value = _money(position.foreign_amount * rate_row.rate)
    difference = _money(new_value - previous)
    journal = None
    if difference:
        from accounting.services import get_account_for_role, post_journal
        entry = JournalEntry.objects.create(
            company=account.company, branch=branch, fiscal_year=year,
            entry_number=f"FX-REV-{account.pk}-{revaluation_date:%Y%m%d}",
            entry_date=revaluation_date,
            description=f"إعادة تقييم {account.name_ar} بعملة {account.currency_code}",
            source_type=JournalEntry.Source.FX_REVALUATION, created_by=user,
        )
        if difference > 0:
            counterpart = get_account_for_role(account.company, "unrealized_fx_gain")
            lines = [
                JournalLine(entry=entry, account=account.account, debit=difference, credit=0,
                            description="زيادة قيمة مركز العملة الأجنبية"),
                JournalLine(entry=entry, account=counterpart, debit=0, credit=difference,
                            description="أرباح فروق عملة غير محققة"),
            ]
        else:
            counterpart = get_account_for_role(account.company, "unrealized_fx_loss")
            lines = [
                JournalLine(entry=entry, account=counterpart, debit=-difference, credit=0,
                            description="خسائر فروق عملة غير محققة"),
                JournalLine(entry=entry, account=account.account, debit=0, credit=-difference,
                            description="انخفاض قيمة مركز العملة الأجنبية"),
            ]
        JournalLine.objects.bulk_create(lines)
        journal = post_journal(entry.pk, actor=user)
    position.carrying_base_amount = new_value
    position.save(update_fields=("carrying_base_amount", "updated_at"))
    return TreasuryFxRevaluation.objects.create(
        company=account.company, account=account, fiscal_year=year,
        revaluation_date=revaluation_date, rate_date=rate_date, rate=rate_row.rate,
        foreign_amount=position.foreign_amount, previous_carrying_amount=previous,
        new_carrying_amount=new_value, difference=difference, journal_entry=journal,
    )
