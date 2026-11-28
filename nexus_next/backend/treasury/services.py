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


def reverse_linked_document_treasury_movement(*, journal_entry_id, reversal_journal_entry, user=None):
    """Mark a bank mirror for an operational receipt reversed with its source journal."""
    from .models import BankStatementLine, BankStatementMatchAllocation, TreasuryCheque

    movement = TreasuryTransaction.objects.select_for_update().filter(
        journal_entry_id=journal_entry_id,
    ).first()
    if movement is None:
        return None
    if movement.status != TreasuryTransaction.Status.POSTED or movement.reversal_journal_entry_id:
        raise ValidationError("حركة البنك المرتبطة بالمستند ليست في حالة تسمح بالعكس.")
    if (
        BankStatementLine.objects.filter(matched_transaction_id=movement.pk).exists()
        or BankStatementMatchAllocation.objects.filter(transaction_id=movement.pk).exists()
    ):
        raise ValidationError("لا يمكن عكس المستند بعد مطابقة حركته بنكيًا؛ فك المطابقة أولًا.")
    if TreasuryCheque.objects.filter(treasury_transaction_id=movement.pk).exists():
        raise ValidationError("لا يمكن عكس المستند المرتبط بشيك؛ عالج دورة الشيك أولًا.")

    TreasuryTransaction.objects.filter(
        pk=movement.pk, status=TreasuryTransaction.Status.POSTED,
    ).update(
        status=TreasuryTransaction.Status.REVERSED,
        reversal_journal_entry=reversal_journal_entry,
        reversed_at=timezone.now(),
        reversed_by=user,
    )
    movement.refresh_from_db()
    return movement


def _foreign_position(account):
    """قفل مركز العملة، مع قبول الافتتاحي الأجنبي الموثق فقط."""
    created = False
    position = TreasuryFxPosition.objects.select_for_update().filter(account=account).first()
    if position:
        if account.opening_journal_entry_id and account.opening_foreign_amount <= 0:
            raise ValidationError("القيد الافتتاحي القديم غير موثق بمبلغ أجنبي وسعر صرف؛ راجع الرصيد قبل المتابعة.")
        if account.opening_foreign_amount > 0 and not account.opening_journal_entry_id:
            raise ValidationError("الرصيد الأجنبي الافتتاحي غير مرتبط بقيد افتتاحي مرحّل.")
    else:
        if account.opening_balance != 0 or account.opening_journal_entry_id or account.opening_foreign_amount > 0:
            raise ValidationError(
                "يوجد رصيد افتتاحي غير ممثل بمركز عملة موثق؛ راجع القيد الافتتاحي قبل المتابعة."
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
    """تسجيل مرآة لحركة خزينة أو بنك لمستند مرحّل وربطها بالقيد الأصلي دون قيد مكرر."""
    if kind not in (TreasuryTransaction.Kind.RECEIPT, TreasuryTransaction.Kind.PAYMENT):
        raise ValidationError("نوع حركة الخزينة المرتبطة بالمستند غير مدعوم.")
    treasury_account = TreasuryAccount.objects.select_for_update().filter(
        company=company, account=account, kind__in=(
            TreasuryAccount.Kind.CASH, TreasuryAccount.Kind.BANK,
        ), is_active=True,
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
        ).filter(
            Q(reversal_journal_entry__isnull=True)
            | Q(reversal_journal_entry__status=JournalEntry.Status.REVERSED)
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
        if doc.kind in (TreasuryTransaction.Kind.PAYMENT, TreasuryTransaction.Kind.TRANSFER):
            balance_movements = source.account.journal_lines.filter(
                entry__status__in=(JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED)
            ).values("entry_id", "entry__entry_date").annotate(
                debit=Sum("debit"), credit=Sum("credit"),
            ).order_by("entry__entry_date", "entry_id")
            available = (
                Decimal("0") if source.opening_journal_entry_id
                else source.opening_balance
            )
            later_movements = []
            for movement in balance_movements:
                change = (
                    (movement["debit"] or Decimal("0"))
                    - (movement["credit"] or Decimal("0"))
                )
                if movement["entry__entry_date"] <= doc.transaction_date:
                    available += change
                else:
                    later_movements.append(change)

            if available < doc.amount:
                raise ValidationError(
                    f"الرصيد المتاح في تاريخ الحركة ({available}) أقل من مبلغ الحركة ({doc.amount})."
                )
            for change in later_movements:
                available += change
                if available < doc.amount:
                    raise ValidationError(
                        "لا يمكن ترحيل حركة بتاريخ سابق؛ ستؤدي إلى رصيد خزينة/بنك سالب بعد حركة لاحقة مسجلة."
                    )

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
            if foreign:
                raise ValidationError("شراء أصل بضريبة مدخلات قابلة للاسترداد يتطلب حساب خزينة بعملة الشركة الأساسية لضمان توزيع لقطة سعر الصرف بدقة.")
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
        valid_opening = (
            account.opening_foreign_amount > 0
            and account.opening_exchange_rate is not None
            and account.opening_exchange_rate_date is not None
            and account.opening_journal_entry_id is not None
            and TreasuryFxPosition.objects.filter(account=account).exists()
        )
        if not valid_opening:
            raise ValidationError("الحساب له رصيد افتتاحي غير ممثل بمركز عملة أجنبي موثق؛ عالج الرصيد الافتتاحي أولًا.")
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
    if TreasuryFxRevaluation.objects.filter(
        account=account, revaluation_date__gt=revaluation_date,
    ).filter(
        # التقييم المعكوس لا يمنع تقييمًا بتاريخ رجعي — نظير فحص الفواتير
        # (fx_subledger_services.py) الذي يستثني المعكوسة (NEW-6-2).
        Q(reversal_journal_entry__isnull=True)
        | Q(reversal_journal_entry__status=JournalEntry.Status.REVERSED),
    ).exists():
        raise ValidationError("يوجد تقييم أحدث؛ لا يمكن إنشاء تقييم بتاريخ رجعي.")
    if TreasuryFxRevaluation.objects.filter(
        account=account,
        reversal_journal_entry__isnull=False,
        reversal_journal_entry__entry_date__gte=revaluation_date,
        reversal_journal_entry__status=JournalEntry.Status.POSTED,
    ).exists():
        raise ValidationError("يوجد عكس تقييم بتاريخ لاحق؛ لا يمكن إنشاء تقييم قبل تاريخ العكس.")
    if TreasuryFxRevaluation.objects.filter(
        account=account, revaluation_date=revaluation_date,
    ).filter(
        # سجل بلا قيد (فرق صفر — NEW-7-1) أو سجل معكوس لا يمنع تقييمًا
        # جديدًا في نفس التاريخ.
        Q(journal_entry__isnull=False),
        Q(reversal_journal_entry__isnull=True)
        | Q(reversal_journal_entry__status=JournalEntry.Status.REVERSED),
    ).exists():
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
    if not difference:
        # فرق صفر: لا ننشئ سجل تقييم بلا قيد (NEW-7-1؛ وهو أيضًا أصل
        # NEW-3-12). نعيد نسخة غير محفوظة بنفس القيم؛ فيبقى عقد
        # الاستجابة كما هو مع status "no_difference" وdifference 0.
        return TreasuryFxRevaluation(
            company=account.company, account=account, fiscal_year=year,
            revaluation_date=revaluation_date, rate_date=rate_date, rate=rate_row.rate,
            foreign_amount=position.foreign_amount, previous_carrying_amount=previous,
            new_carrying_amount=new_value, difference=difference, journal_entry=None,
        )
    return TreasuryFxRevaluation.objects.create(
        company=account.company, account=account, fiscal_year=year,
        revaluation_date=revaluation_date, rate_date=rate_date, rate=rate_row.rate,
        foreign_amount=position.foreign_amount, previous_carrying_amount=previous,
        new_carrying_amount=new_value, difference=difference, journal_entry=journal,
    )



def _treasury_fx_revaluation_reversal_blocker(revaluation, *, account=None, position=None, entry_date=None):
    """Return a safe, user-facing reason when a treasury FX revaluation is not reversible."""
    if not revaluation.journal_entry_id:
        return "إعادة التقييم دون فرق محاسبي ولا يوجد قيد يمكن عكسه."
    if revaluation.reversal_journal_entry_id or revaluation.reversed_at:
        return "تم عكس إعادة التقييم من قبل."
    if revaluation.journal_entry.status != JournalEntry.Status.POSTED:
        return "قيد إعادة التقييم الأصلي ليس في حالة ترحيل تسمح بالعكس."
    if entry_date and entry_date < revaluation.revaluation_date:
        return "تاريخ قيد العكس لا يجوز أن يسبق تاريخ إعادة التقييم."

    account = account or revaluation.account
    position = position or TreasuryFxPosition.objects.filter(account=account).first()
    if not position:
        return "مركز العملة غير موجود؛ لا يمكن تنفيذ العكس بأمان."
    if (
        position.foreign_amount != revaluation.foreign_amount
        or position.carrying_base_amount != revaluation.new_carrying_amount
    ):
        return "مركز العملة تغير بعد هذا التقييم؛ لا يمكن عكس تقييم أقدم قبل معالجة الأحداث اللاحقة."

    later_revaluations = TreasuryFxRevaluation.objects.filter(
        account=account,
        revaluation_date__gt=revaluation.revaluation_date,
        difference__gt=0,
        reversal_journal_entry__isnull=True,
    ).exists() or TreasuryFxRevaluation.objects.filter(
        account=account,
        revaluation_date__gt=revaluation.revaluation_date,
        difference__lt=0,
        reversal_journal_entry__isnull=True,
    ).exists()
    if later_revaluations:
        return "يوجد تقييم أحدث غير معكوس؛ ابدأ بعكس آخر تقييم أولًا."

    later_movements = TreasuryTransaction.objects.filter(
        Q(source_account=account) | Q(destination_account=account),
        status=TreasuryTransaction.Status.POSTED,
    ).filter(
        Q(transaction_date__gt=revaluation.revaluation_date)
        | Q(
            transaction_date=revaluation.revaluation_date,
            posted_at__isnull=True,
        )
        | Q(
            transaction_date=revaluation.revaluation_date,
            posted_at__gt=revaluation.created_at,
        )
    ).exists()
    if later_movements:
        return "توجد حركة خزينة مرحّلة بعد إعادة التقييم؛ لا يمكن عكسه قبل معالجة الحركة اللاحقة."

    if entry_date:
        later_reversal_events = TreasuryFxRevaluation.objects.filter(
            account=account,
            revaluation_date__gt=revaluation.revaluation_date,
            reversal_journal_entry__isnull=False,
            reversal_journal_entry__entry_date__gte=entry_date,
            reversal_journal_entry__status=JournalEntry.Status.POSTED,
        ).exists()
        if later_reversal_events:
            return "تاريخ العكس يسبق أو يساوي عكس تقييم أحدث؛ التزم بعكس الأحداث بترتيبها الزمني."
    return None


def can_reverse_treasury_fx_revaluation(revaluation, *, entry_date=None):
    """Read-only eligibility check used by the revaluation history screen."""
    return _treasury_fx_revaluation_reversal_blocker(
        revaluation, entry_date=entry_date or timezone.localdate(),
    ) is None


@transaction.atomic
def reverse_treasury_fx_revaluation(*, revaluation_id, entry_date, user=None):
    """Reverse the latest safe treasury FX revaluation and restore its subledger carrying value."""
    revaluation = TreasuryFxRevaluation.objects.select_for_update(of=("self",)).select_related(
        "journal_entry", "account",
    ).get(pk=revaluation_id)
    account = TreasuryAccount.objects.select_for_update(of=("self",)).get(pk=revaluation.account_id)
    position = TreasuryFxPosition.objects.select_for_update().filter(account=account).first()
    blocker = _treasury_fx_revaluation_reversal_blocker(
        revaluation, account=account, position=position, entry_date=entry_date,
    )
    if blocker:
        raise ValidationError(blocker)
    if entry_date < revaluation.revaluation_date:
        raise ValidationError("تاريخ قيد العكس لا يجوز أن يسبق تاريخ إعادة التقييم.")

    from accounting.services import reverse_operational_journal
    reversal = reverse_operational_journal(
        revaluation.journal_entry_id, user, entry_date,
        expected_source=JournalEntry.Source.FX_REVALUATION,
    )
    position.carrying_base_amount = revaluation.previous_carrying_amount
    position.save(update_fields=("carrying_base_amount", "updated_at"))
    revaluation.reversal_journal_entry = reversal
    revaluation.reversed_at = timezone.now()
    revaluation.reversed_by = user
    revaluation.save(update_fields=(
        "reversal_journal_entry", "reversed_at", "reversed_by",
    ))
    return revaluation


def _foreign_treasury_transaction_reversal_blocker(doc, accounts):
    """Prevent reversing an FX movement while a later live subledger event depends on it."""
    for account in accounts:
        later_movements = TreasuryTransaction.objects.filter(
            status=TreasuryTransaction.Status.POSTED,
        ).filter(
            Q(source_account=account) | Q(destination_account=account),
        ).exclude(pk=doc.pk).filter(posted_at__gt=doc.posted_at)
        if later_movements.exists():
            return "توجد حركة خزينة أجنبية رُحّلت بعد هذا المستند؛ اعكس الحركات الأحدث أولًا حتى لا يتشوّه مركز العملة."
        later_revaluations = TreasuryFxRevaluation.objects.filter(
            account=account, created_at__gt=doc.posted_at,
        ).filter(
            Q(reversal_journal_entry__isnull=True)
            | Q(reversal_journal_entry__status=JournalEntry.Status.REVERSED)
        )
        if later_revaluations.exists():
            return "يوجد تقييم عملة أحدث من الحركة؛ اعكس إعادة التقييم الأحدث أولًا قبل عكس سند الخزينة."
    return None


@transaction.atomic
def reverse_treasury_transaction(*, transaction_id, entry_date, user=None):
    """Reverse a standalone treasury document and restore any foreign-currency subledger positions."""
    doc = TreasuryTransaction.objects.select_for_update(of=("self",)).select_related(
        "company__base_currency", "branch", "source_account", "destination_account", "journal_entry",
        "transaction_currency",
    ).get(pk=transaction_id)
    if doc.status != TreasuryTransaction.Status.POSTED:
        raise ValidationError("يمكن عكس مستند خزينة مرحّل فقط.")
    if not doc.journal_entry_id or doc.journal_entry.status != JournalEntry.Status.POSTED:
        raise ValidationError("القيد الأصلي غير موجود أو ليس مرحّلًا؛ لا يمكن عكس المستند.")
    if doc.journal_entry.source_type != JournalEntry.Source.MANUAL:
        raise ValidationError("هذه حركة مرتبطة بمستند تشغيلي؛ يجب عكسها من الوحدة الأصلية وليس من الخزينة.")
    if doc.reversal_journal_entry_id:
        raise ValidationError("تم عكس مستند الخزينة من قبل.")
    base_code = doc.company.base_currency.code
    foreign = doc.source_account.currency_code != base_code
    if (doc.destination_account_id and doc.destination_account.currency_code != doc.source_account.currency_code):
        raise ValidationError("عملة حساب التحويل لا تطابق عملة الحساب المصدر؛ لا يمكن عكس الحركة بأمان.")
    if foreign and not doc.posted_at:
        raise ValidationError("تاريخ ترحيل المستند غير متوفر للتحقق من تسلسل مركز العملة؛ راجع سجل المستند.")
    if foreign and (
        not doc.transaction_currency_id or doc.transaction_currency.code != doc.source_account.currency_code
        or not doc.transaction_amount or not doc.exchange_rate or not doc.carrying_amount
        or doc.transaction_amount <= 0 or doc.exchange_rate <= 0 or doc.carrying_amount <= 0
        or doc.amount != _money(doc.transaction_amount * doc.exchange_rate)
    ):
        raise ValidationError("بيانات المبلغ الأجنبي أو القيمة الدفترية الأصلية غير مكتملة؛ راجع المستند قبل العكس.")
    if not foreign and (
        doc.transaction_currency_id is not None or doc.transaction_amount is not None
        or doc.exchange_rate is not None or doc.carrying_amount is not None
    ):
        raise ValidationError("بيانات العملة الأجنبية لا تتفق مع عملة حساب الخزينة.")
    from hr.models import PayrollPosting
    if PayrollPosting.objects.filter(payment_treasury_transaction_id=doc.pk).exists():
        raise ValidationError("حركة صرف المرتبات مرتبطة بدورة المرتبات؛ عالجها من شاشة المرتبات.")
    from assets.models import FixedAsset
    if FixedAsset.objects.filter(Q(acquisition_treasury_transaction_id=doc.pk) | Q(disposal_treasury_transaction_id=doc.pk)).exists():
        raise ValidationError("حركة اقتناء أو استبعاد أصل ثابت؛ عالجها من دورة الأصول.")
    from .models import BankStatementLine, BankStatementMatchAllocation
    if (
        BankStatementLine.objects.filter(matched_transaction_id=doc.pk).exists()
        or BankStatementMatchAllocation.objects.filter(transaction_id=doc.pk).exists()
    ):
        raise ValidationError("الحركة مرتبطة بتسوية بنكية؛ فك المطابقة أو عالج التسوية أولًا قبل العكس.")
    if entry_date < doc.transaction_date:
        raise ValidationError("تاريخ العكس لا يجوز أن يسبق تاريخ مستند الخزينة الأصلي.")

    positions = {}
    if foreign:
        account_ids = {doc.source_account_id}
        if doc.destination_account_id:
            account_ids.add(doc.destination_account_id)
        locked_accounts = {
            account.pk: account
            for account in TreasuryAccount.objects.select_for_update(of=("self",))
            .select_related("company__base_currency", "account")
            .filter(pk__in=sorted(account_ids)).order_by("pk")
        }
        blocker = _foreign_treasury_transaction_reversal_blocker(
            doc, [locked_accounts[key] for key in sorted(account_ids)],
        )
        if blocker:
            raise ValidationError(blocker)
        positions = {
            account_id: _foreign_position(locked_accounts[account_id])
            for account_id in sorted(account_ids)
        }
        source_position = positions[doc.source_account_id]
        amount = doc.transaction_amount
        carrying = doc.carrying_amount
        if doc.kind == TreasuryTransaction.Kind.RECEIPT:
            if source_position.foreign_amount < amount or source_position.carrying_base_amount < carrying:
                raise ValidationError("تم استخدام جزء من رصيد القبض بعد الترحيل؛ راجع الحركات اللاحقة قبل العكس.")
            source_position.foreign_amount -= amount
            source_position.carrying_base_amount -= carrying
        elif doc.kind == TreasuryTransaction.Kind.PAYMENT:
            source_position.foreign_amount += amount
            source_position.carrying_base_amount += carrying
        else:
            destination_position = positions[doc.destination_account_id]
            if (
                destination_position.foreign_amount < amount
                or destination_position.carrying_base_amount < carrying
            ):
                raise ValidationError("رصيد الحساب المستقبِل تغيّر؛ لا يمكن عكس التحويل دون التسبب في رصيد أجنبي سالب.")
            source_position.foreign_amount += amount
            source_position.carrying_base_amount += carrying
            destination_position.foreign_amount -= amount
            destination_position.carrying_base_amount -= carrying
        for position in positions.values():
            if position.foreign_amount < 0 or position.carrying_base_amount < 0:
                raise ValidationError("عكس الحركة سيؤدي إلى رصيد أجنبي أو قيمة دفترية سالبة.")
            if (position.foreign_amount == 0) != (position.carrying_base_amount == 0):
                raise ValidationError("عكس الحركة سيترك مركز العملة غير متوازن؛ راجع سجل الحركات.")
    from accounting.services import reverse_operational_journal
    reversal = reverse_operational_journal(
        doc.journal_entry_id, user, entry_date, expected_source=JournalEntry.Source.MANUAL,
    )
    for position in positions.values():
        position.save(update_fields=("foreign_amount", "carrying_base_amount", "updated_at"))
    TreasuryTransaction.objects.filter(pk=doc.pk, status=TreasuryTransaction.Status.POSTED).update(
        status=TreasuryTransaction.Status.REVERSED, reversal_journal_entry=reversal,
        reversed_at=timezone.now(), reversed_by=user,
    )
    doc.refresh_from_db()
    return doc
