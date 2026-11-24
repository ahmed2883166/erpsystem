"""خدمات اقتناء الأصول الثابتة واحتساب الإهلاك والتصرف والبيع مع القيود المحاسبية."""

from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
from django.core.exceptions import ValidationError, ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone, dateparse
from django.db.models import Sum

from accounting.models import AccountRole, JournalEntry, JournalLine
from accounting.services import get_account_for_role, post_journal
from companies.models import FiscalYear
from core.money import round_currency_amount
from .models import FixedAsset, AssetDepreciation, AssetImpairment, AssetPurchaseInvoice, AssetSupplierPayment


MONEY = Decimal("0.0001")


def _post_entry(asset, fiscal_year, entry_number, entry_date, description, lines, user=None):
    """إنشاء وترحيل قيد محاسبي مرتبط بحركة الأصل الثابت."""
    entry = JournalEntry.objects.create(
        company=asset.company, branch=asset.branch, fiscal_year=fiscal_year,
        entry_number=entry_number, entry_date=entry_date, description=description,
        source_type=JournalEntry.Source.ASSETS, created_by=user,
    )
    JournalLine.objects.bulk_create([JournalLine(entry=entry, **line) for line in lines])
    return post_journal(entry.pk, actor=user)


@transaction.atomic
def acquire_asset(asset_id, fiscal_year_id, user=None):
    """إثبات اقتناء الأصل وتكلفته في الحسابات والسنة المالية المحددة."""
    asset = FixedAsset.objects.select_for_update().select_related(
        "company", "branch", "asset_account", "acquisition_counter_account"
    ).get(pk=asset_id)
    if asset.acquisition_journal_id:
        raise ValidationError("تم تسجيل قيد اقتناء الأصل بالفعل.")
    try:
        year = FiscalYear.objects.get(pk=fiscal_year_id, company=asset.company, status=FiscalYear.Status.OPEN)
    except (FiscalYear.DoesNotExist, TypeError, ValueError):
        raise ValidationError("حدد سنة مالية مفتوحة وصحيحة للشركة.") from None
    if not (year.start_date <= asset.acquisition_date <= year.end_date):
        raise ValidationError("تاريخ الاقتناء خارج السنة المالية المفتوحة.")

    recoverable_tax = asset.recoverable_tax_amount or Decimal("0")
    nonrecoverable_tax = asset.nonrecoverable_tax_amount or Decimal("0")
    if recoverable_tax < 0 or nonrecoverable_tax < 0:
        raise ValidationError("ضريبة اقتناء الأصل لا يمكن أن تكون سالبة.")
    if asset.acquisition_tax_code_id:
        tax_code = asset.acquisition_tax_code
        if (
            not tax_code.is_active or tax_code.company_id != asset.company_id
            or tax_code.country != asset.company.country or tax_code.category != "standard"
            or tax_code.effective_from > asset.acquisition_date
            or (tax_code.effective_to and tax_code.effective_to < asset.acquisition_date)
        ):
            raise ValidationError("كود ضريبة اقتناء الأصل غير صالح بتاريخ الاقتناء.")
        if asset.acquisition_net_amount is None:
            raise ValidationError("يجب تحديد صافي تكلفة الأصل قبل الضريبة.")
        expected_tax = round_currency_amount(
            asset.acquisition_net_amount * tax_code.rate / Decimal("100"), asset.company.base_currency,
        )
        expected_recoverable = expected_tax if tax_code.input_tax_recoverable else Decimal("0")
        expected_nonrecoverable = expected_tax - expected_recoverable
        if recoverable_tax != expected_recoverable or nonrecoverable_tax != expected_nonrecoverable:
            raise ValidationError("مبالغ ضريبة الاقتناء لا تطابق كود الضريبة.")
        if asset.original_cost != asset.acquisition_net_amount + nonrecoverable_tax:
            raise ValidationError("تكلفة الأصل لا تطابق الصافي والضريبة غير القابلة للاسترداد.")
    elif recoverable_tax or nonrecoverable_tax:
        raise ValidationError("لا يمكن ترحيل ضريبة اقتناء دون كود ضريبي.")
    vat_input = get_account_for_role(asset.company, AccountRole.Role.VAT_INPUT) if recoverable_tax else None
    if vat_input and (
        vat_input.pk in (asset.asset_account_id, asset.acquisition_counter_account_id)
        or vat_input.nature != "asset" or not vat_input.is_active
        or vat_input.is_group or not vat_input.allow_posting
    ):
        raise ValidationError("حساب ضريبة المدخلات غير صالح أو غير مستقل عن حسابات الأصل والبنك.")
    cash_amount = asset.original_cost + recoverable_tax

    from treasury.models import TreasuryAccount, TreasuryTransaction
    treasury_account = TreasuryAccount.objects.filter(
        company=asset.company, account=asset.acquisition_counter_account, is_active=True
    ).first()
    if not treasury_account:
        raise ValidationError("اربط حساب البنك أو الخزينة بحساب خزينة نشط حتى يُسجل التدفق الاستثماري ومطابقة النقدية بصورة صحيحة.")
    if treasury_account:
        if treasury_account.branch_id and treasury_account.branch_id != asset.branch_id:
            raise ValidationError("حساب الخزينة المرتبط بمقابل الاقتناء لا يتبع فرع الأصل.")
        if asset.acquisition_treasury_transaction_id:
            treasury_doc = TreasuryTransaction.objects.select_for_update().get(
                pk=asset.acquisition_treasury_transaction_id
            )
        else:
            treasury_doc = TreasuryTransaction.objects.create(
                company=asset.company, branch=asset.branch, fiscal_year=year,
                source_account=treasury_account, counter_account=asset.asset_account,
                kind=TreasuryTransaction.Kind.PAYMENT,
                document_number=f"FA-ACQ-{asset.pk}", transaction_date=asset.acquisition_date,
                amount=cash_amount, description=f"سداد تكلفة اقتناء الأصل: {asset.name_ar}",
                cash_flow_category=TreasuryTransaction.CashFlowCategory.INVESTING,
                created_by=user,
            )
            asset.acquisition_treasury_transaction = treasury_doc
            asset.save(update_fields=("acquisition_treasury_transaction",))
            from approvals.models import FinancialApprovalRequest
            from approvals.services import refresh_financial_approval
            approval = refresh_financial_approval(
                FinancialApprovalRequest.DocumentType.TREASURY_PAYMENT, treasury_doc, user
            )
            if approval:
                return asset

        from treasury.services import post_treasury_transaction
        treasury_doc = post_treasury_transaction(treasury_doc.pk, user)
        asset.acquisition_journal = treasury_doc.journal_entry
        asset.save(update_fields=("acquisition_journal",))
        return asset

    raise ValidationError("لم يتم إنشاء حركة خزينة للاقتناء؛ راجع ربط حساب البنك أو الخزينة.")


@transaction.atomic
def depreciate_asset(asset_id, period_date, fiscal_year_id, user=None):
    """احتساب وترحيل إهلاك الأصل عن الفترة حتى تاريخها المحدد."""
    if isinstance(period_date, str):
        period_date = dateparse.parse_date(period_date)
    if not period_date:
        raise ValidationError("أدخل تاريخ فترة إهلاك صحيحًا.")
    asset = FixedAsset.objects.select_for_update().get(pk=asset_id)
    if asset.status != FixedAsset.Status.ACTIVE or not asset.acquisition_journal_id:
        raise ValidationError("الأصل غير نشط أو لم يتم تسجيل اقتنائه.")
    if period_date < asset.depreciation_start_date:
        raise ValidationError("تاريخ الإهلاك يسبق بداية الإهلاك المحددة.")
    if asset.impairments.filter(
        journal_entry__status=JournalEntry.Status.POSTED, impairment_date__gt=period_date,
    ).exists() or asset.impairment_reversals.filter(
        journal_entry__status=JournalEntry.Status.POSTED, reversal_date__gt=period_date,
    ).exists():
        raise ValidationError("يوجد اختبار انخفاض أو عكس انخفاض بتاريخ لاحق؛ لا يمكن ترحيل إهلاك بأثر رجعي قبله.")
    if AssetDepreciation.objects.filter(asset=asset, period_date__year=period_date.year, period_date__month=period_date.month).exists():
        raise ValidationError("تم تسجيل إهلاك هذا الأصل لهذا الشهر من قبل.")
    try:
        year = FiscalYear.objects.get(pk=fiscal_year_id, company=asset.company, status=FiscalYear.Status.OPEN)
    except (FiscalYear.DoesNotExist, TypeError, ValueError):
        raise ValidationError("حدد سنة مالية مفتوحة وصحيحة للشركة.") from None
    if not (year.start_date <= period_date <= year.end_date):
        raise ValidationError("تاريخ الإهلاك خارج السنة المالية المفتوحة.")
    last_depreciation = asset.depreciations.filter(
        journal_entry__status=JournalEntry.Status.POSTED, period_date__lte=period_date,
    ).order_by("-period_date", "-pk").first()
    latest_impairment = asset.impairments.filter(
        journal_entry__status=JournalEntry.Status.POSTED, impairment_date__lte=period_date,
    ).order_by("-impairment_date", "-pk").first()
    latest_reversal = asset.impairment_reversals.filter(
        journal_entry__status=JournalEntry.Status.POSTED, reversal_date__lte=period_date,
    ).order_by("-reversal_date", "-pk").first()
    latest_measurement_date = max(
        latest_impairment.impairment_date if latest_impairment else asset.depreciation_start_date - timedelta(days=1),
        latest_reversal.reversal_date if latest_reversal else asset.depreciation_start_date - timedelta(days=1),
    )
    previous_date = max(
        last_depreciation.period_date if last_depreciation else asset.depreciation_start_date - timedelta(days=1),
        latest_measurement_date,
    )
    elapsed_days = (period_date - previous_date).days
    if elapsed_days <= 0:
        raise ValidationError("تاريخ الإهلاك يجب أن يكون بعد آخر إهلاك أو اختبار انخفاض قيمة.")
    posted_total = asset.depreciations.filter(
        journal_entry__status=JournalEntry.Status.POSTED, period_date__lte=period_date,
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0")
    impairment_total = asset.impairments.filter(
        journal_entry__status=JournalEntry.Status.POSTED, impairment_date__lte=period_date,
    ).aggregate(total=Sum("impairment_loss"))["total"] or Decimal("0")
    reversal_total = asset.impairment_reversals.filter(
        journal_entry__status=JournalEntry.Status.POSTED, reversal_date__lte=period_date,
    ).aggregate(total=Sum("reversal_amount"))["total"] or Decimal("0")
    net_impairment = impairment_total - reversal_total
    remaining = max(Decimal("0"), asset.original_cost - posted_total - net_impairment - asset.salvage_value)
    if remaining <= 0:
        raise ValidationError("تم استهلاك أو تخفيض كامل القيمة القابلة للإهلاك.")
    if latest_measurement_date >= asset.depreciation_start_date:
        dep_at_measurement = asset.depreciations.filter(
            journal_entry__status=JournalEntry.Status.POSTED, period_date__lte=latest_measurement_date,
        ).aggregate(total=Sum("amount"))["total"] or Decimal("0")
        loss_at_measurement = asset.impairments.filter(
            journal_entry__status=JournalEntry.Status.POSTED, impairment_date__lte=latest_measurement_date,
        ).aggregate(total=Sum("impairment_loss"))["total"] or Decimal("0")
        reversal_at_measurement = asset.impairment_reversals.filter(
            journal_entry__status=JournalEntry.Status.POSTED, reversal_date__lte=latest_measurement_date,
        ).aggregate(total=Sum("reversal_amount"))["total"] or Decimal("0")
        carrying_at_measurement = asset.original_cost - dep_at_measurement - loss_at_measurement + reversal_at_measurement
        life_used_days = max(0, (latest_measurement_date - asset.depreciation_start_date).days + 1)
        remaining_life_days = max(1, asset.useful_life_months * 30 - life_used_days)
        revised_basis = max(Decimal("0"), carrying_at_measurement - asset.salvage_value)
        daily_rate = revised_basis / Decimal(remaining_life_days)
    else:
        daily_rate = asset.depreciable_amount / Decimal(asset.useful_life_months * 30)
    amount = min(round_currency_amount(daily_rate * Decimal(elapsed_days), asset.company.base_currency), remaining)
    entry = _post_entry(asset, year, f"FA-DEP-{asset.pk}-{period_date:%Y%m}", period_date,
        f"إهلاك الأصل: {asset.name_ar} - {period_date:%Y-%m}", [
            {"account": asset.depreciation_expense_account, "debit": amount, "credit": 0, "description": asset.name_ar},
            {"account": asset.accumulated_depreciation_account, "debit": 0, "credit": amount, "description": asset.name_ar},
        ], user)
    AssetDepreciation.objects.create(asset=asset, period_date=period_date, amount=amount, journal_entry=entry)
    return amount


@transaction.atomic
def record_asset_impairment(asset_id, impairment_date, recoverable_amount, impairment_expense_account_id,
                            accumulated_impairment_account_id, fiscal_year_id, user=None):
    """إثبات خسارة انخفاض القيمة وفق IAS 36 بعد ترحيل إهلاك تاريخ الاختبار."""
    if isinstance(impairment_date, str):
        impairment_date = dateparse.parse_date(impairment_date)
    if not impairment_date:
        raise ValidationError("أدخل تاريخ اختبار انخفاض قيمة صحيحًا.")
    try:
        recoverable_amount = Decimal(str(recoverable_amount))
    except Exception as exc:
        raise ValidationError("القيمة القابلة للاسترداد غير صحيحة.") from exc
    if recoverable_amount < 0:
        raise ValidationError("القيمة القابلة للاسترداد لا يمكن أن تكون سالبة.")

    asset = FixedAsset.objects.select_for_update().select_related(
        "company", "branch", "asset_account", "accumulated_depreciation_account",
    ).get(pk=asset_id)
    if asset.status != FixedAsset.Status.ACTIVE or not asset.acquisition_journal_id:
        raise ValidationError("لا يمكن اختبار انخفاض أصل غير نشط أو لم يتم إثبات اقتنائه.")
    recoverable_amount = round_currency_amount(recoverable_amount, asset.company.base_currency)
    if impairment_date < asset.acquisition_date:
        raise ValidationError("تاريخ اختبار الانخفاض يسبق تاريخ اقتناء الأصل.")
    if AssetImpairment.objects.filter(asset=asset, impairment_date=impairment_date).exists():
        raise ValidationError("تم تسجيل اختبار انخفاض قيمة لهذا الأصل في التاريخ نفسه.")
    if asset.impairments.filter(
        journal_entry__status=JournalEntry.Status.POSTED, impairment_date__gt=impairment_date,
    ).exists() or asset.impairment_reversals.filter(
        journal_entry__status=JournalEntry.Status.POSTED, reversal_date__gt=impairment_date,
    ).exists():
        raise ValidationError("يوجد اختبار انخفاض أو عكس انخفاض بتاريخ لاحق؛ يجب معالجة الأحداث بترتيبها الزمني.")
    if asset.depreciations.filter(
        journal_entry__status=JournalEntry.Status.POSTED, period_date__gt=impairment_date,
    ).exists():
        raise ValidationError("يوجد إهلاك بتاريخ لاحق لاختبار الانخفاض؛ راجع التسلسل الزمني أولًا.")
    last_depreciation = asset.depreciations.filter(
        journal_entry__status=JournalEntry.Status.POSTED, period_date__lte=impairment_date,
    ).order_by("-period_date", "-pk").first()
    if impairment_date >= asset.depreciation_start_date and (
        not last_depreciation or last_depreciation.period_date < impairment_date
    ):
        raise ValidationError("رحّل إهلاك الأصل حتى تاريخ اختبار الانخفاض أولًا.")

    try:
        year = FiscalYear.objects.get(pk=fiscal_year_id, company=asset.company, status=FiscalYear.Status.OPEN)
    except (FiscalYear.DoesNotExist, TypeError, ValueError):
        raise ValidationError("حدد سنة مالية مفتوحة وصحيحة.") from None
    if not (year.start_date <= impairment_date <= year.end_date):
        raise ValidationError("تاريخ اختبار الانخفاض خارج السنة المالية المفتوحة.")

    depreciation_total = asset.depreciations.filter(
        journal_entry__status=JournalEntry.Status.POSTED, period_date__lte=impairment_date,
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0")
    previous_impairment_total = asset.impairments.filter(
        journal_entry__status=JournalEntry.Status.POSTED, impairment_date__lte=impairment_date,
    ).aggregate(total=Sum("impairment_loss"))["total"] or Decimal("0")
    previous_reversal_total = asset.impairment_reversals.filter(
        journal_entry__status=JournalEntry.Status.POSTED, reversal_date__lte=impairment_date,
    ).aggregate(total=Sum("reversal_amount"))["total"] or Decimal("0")
    carrying_amount = asset.original_cost - depreciation_total - previous_impairment_total + previous_reversal_total
    if carrying_amount <= 0:
        raise ValidationError("القيمة الدفترية للأصل لا تسمح بإثبات انخفاض إضافي.")
    if recoverable_amount >= carrying_amount:
        raise ValidationError("لا توجد خسارة انخفاض؛ القيمة القابلة للاسترداد تساوي أو تتجاوز القيمة الدفترية.")

    from accounting.models import ChartOfAccount
    try:
        expense_account = ChartOfAccount.objects.get(
            pk=impairment_expense_account_id, company=asset.company, is_active=True,
            is_group=False, allow_posting=True, nature=ChartOfAccount.Nature.EXPENSE,
        )
        contra_account = ChartOfAccount.objects.get(
            pk=accumulated_impairment_account_id, company=asset.company, is_active=True,
            is_group=False, allow_posting=True, nature=ChartOfAccount.Nature.ASSET,
        )
    except (ChartOfAccount.DoesNotExist, TypeError, ValueError):
        raise ValidationError("اختر حساب مصروف انخفاض وحساب مجمع انخفاض صالحين من نفس الشركة.") from None
    if len({asset.asset_account_id, asset.accumulated_depreciation_account_id, expense_account.pk, contra_account.pk}) != 4:
        raise ValidationError("يجب فصل حساب تكلفة الأصل ومجمع الإهلاك ومصروف ومجمع الانخفاض.")

    loss = carrying_amount - recoverable_amount
    entry = _post_entry(
        asset, year, f"FA-IMP-{asset.pk}-{impairment_date:%Y%m%d}", impairment_date,
        f"إثبات انخفاض قيمة الأصل وفق IAS 36: {asset.name_ar}", [
            {"account": expense_account, "debit": loss, "credit": 0, "description": f"خسارة انخفاض قيمة {asset.name_ar}"},
            {"account": contra_account, "debit": 0, "credit": loss, "description": f"مجمع انخفاض قيمة {asset.name_ar}"},
        ], user,
    )
    impairment = AssetImpairment(
        asset=asset, impairment_date=impairment_date,
        carrying_amount_before=carrying_amount, recoverable_amount=recoverable_amount,
        impairment_loss=loss, impairment_expense_account=expense_account,
        accumulated_impairment_account=contra_account, journal_entry=entry,
    )
    impairment.full_clean()
    impairment.save()
    return impairment


@transaction.atomic
def reverse_asset_impairment(asset_id, reversal_date, recoverable_amount, reversal_income_account_id,
                             fiscal_year_id, user=None):
    """Reverse IAS 36 impairment under the cost model, subject to the no-impairment ceiling."""
    from .models import AssetImpairmentReversal, AssetImpairmentReversalAllocation
    from accounting.models import ChartOfAccount

    if isinstance(reversal_date, str):
        reversal_date = dateparse.parse_date(reversal_date)
    if not reversal_date:
        raise ValidationError("أدخل تاريخ عكس انخفاض قيمة صحيحًا.")
    try:
        recoverable_amount = Decimal(str(recoverable_amount))
    except Exception as exc:
        raise ValidationError("القيمة القابلة للاسترداد غير صحيحة.") from exc
    if recoverable_amount < 0:
        raise ValidationError("القيمة القابلة للاسترداد لا يمكن أن تكون سالبة.")

    asset = FixedAsset.objects.select_for_update().select_related(
        "company", "branch", "asset_account", "accumulated_depreciation_account",
    ).get(pk=asset_id)
    if asset.status != FixedAsset.Status.ACTIVE or not asset.acquisition_journal_id:
        raise ValidationError("لا يمكن عكس انخفاض أصل غير نشط أو لم يتم إثبات اقتنائه.")
    if not asset.impairments.filter(
        journal_entry__status=JournalEntry.Status.POSTED, impairment_date__lt=reversal_date,
    ).exists():
        raise ValidationError("لا يوجد انخفاض قيمة سابق مرحّل يمكن عكسه.")
    if AssetImpairmentReversal.objects.filter(asset=asset, reversal_date=reversal_date).exists():
        raise ValidationError("تم تسجيل عكس انخفاض لهذا الأصل في التاريخ نفسه.")
    if asset.impairments.filter(
        journal_entry__status=JournalEntry.Status.POSTED, impairment_date__gt=reversal_date,
    ).exists() or asset.depreciations.filter(
        journal_entry__status=JournalEntry.Status.POSTED, period_date__gt=reversal_date,
    ).exists() or asset.impairment_reversals.filter(
        journal_entry__status=JournalEntry.Status.POSTED, reversal_date__gt=reversal_date,
    ).exists():
        raise ValidationError("يوجد حدث محاسبي لاحق؛ يجب معالجة الأحداث بترتيبها الزمني دون تعديل قيود مرحلة.")
    last_depreciation = asset.depreciations.filter(
        journal_entry__status=JournalEntry.Status.POSTED, period_date__lte=reversal_date,
    ).order_by("-period_date", "-pk").first()
    if reversal_date >= asset.depreciation_start_date and (
        not last_depreciation or last_depreciation.period_date < reversal_date
    ):
        raise ValidationError("رحّل إهلاك الأصل حتى تاريخ عكس الانخفاض أولًا.")

    try:
        year = FiscalYear.objects.get(pk=fiscal_year_id, company=asset.company, status=FiscalYear.Status.OPEN)
    except (FiscalYear.DoesNotExist, TypeError, ValueError):
        raise ValidationError("حدد سنة مالية مفتوحة وصحيحة.") from None
    if not (year.start_date <= reversal_date <= year.end_date):
        raise ValidationError("تاريخ عكس الانخفاض خارج السنة المالية المفتوحة.")

    depreciation_total = asset.depreciations.filter(
        journal_entry__status=JournalEntry.Status.POSTED, period_date__lte=reversal_date,
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0")
    impairment_total = asset.impairments.filter(
        journal_entry__status=JournalEntry.Status.POSTED, impairment_date__lt=reversal_date,
    ).aggregate(total=Sum("impairment_loss"))["total"] or Decimal("0")
    reversal_total = asset.impairment_reversals.filter(
        journal_entry__status=JournalEntry.Status.POSTED, reversal_date__lt=reversal_date,
    ).aggregate(total=Sum("reversal_amount"))["total"] or Decimal("0")
    net_impairment = impairment_total - reversal_total
    carrying_amount = asset.original_cost - depreciation_total - net_impairment
    if carrying_amount < 0:
        raise ValidationError("القيمة الدفترية المحسوبة سالبة؛ راجع سجل الأصل قبل العكس.")

    elapsed_days = max(0, (reversal_date - asset.depreciation_start_date).days + 1)
    total_life_days = asset.useful_life_months * 30
    hypothetical_depreciation = min(
        asset.depreciable_amount,
        round_currency_amount(
            asset.depreciable_amount * Decimal(elapsed_days) / Decimal(total_life_days),
            asset.company.base_currency,
        ),
    )
    hypothetical_carrying = max(asset.salvage_value, asset.original_cost - hypothetical_depreciation)
    reversal_ceiling = max(Decimal("0"), hypothetical_carrying - carrying_amount)
    available_recoverable_increase = max(Decimal("0"), recoverable_amount - carrying_amount)
    currency_places = int(getattr(asset.company.base_currency, "decimal_places", 2))
    currency_quantum = Decimal("1").scaleb(-currency_places)
    reversal_amount = min(reversal_ceiling, available_recoverable_increase).quantize(
        currency_quantum, rounding=ROUND_DOWN,
    )
    if reversal_amount <= 0:
        raise ValidationError(
            "لا توجد قيمة قابلة للعكس؛ إما أن القيمة القابلة للاسترداد لا تتجاوز القيمة الدفترية "
            "أو أن حد IAS 36 الافتراضي دون انخفاض يمنع الزيادة."
        )

    income_account = ChartOfAccount.objects.filter(
        pk=reversal_income_account_id, company=asset.company, nature=ChartOfAccount.Nature.REVENUE,
        is_active=True, is_group=False, allow_posting=True,
    ).first()
    if not income_account:
        raise ValidationError("اختر حساب إيراد لعكس انخفاض القيمة، تفصيليًا ونشطًا وقابلًا للترحيل من نفس الشركة.")

    impairments = list(asset.impairments.select_for_update().filter(
        journal_entry__status=JournalEntry.Status.POSTED, impairment_date__lt=reversal_date,
    ).select_related("accumulated_impairment_account").order_by("-impairment_date", "-pk"))
    remaining_to_allocate = reversal_amount
    debit_by_account = {}
    allocations = []
    for impairment in impairments:
        already_reversed = impairment.reversal_allocations.filter(
            reversal__journal_entry__status=JournalEntry.Status.POSTED,
        ).aggregate(total=Sum("amount"))["total"] or Decimal("0")
        outstanding = impairment.impairment_loss - already_reversed
        if outstanding <= 0:
            continue
        allocated = min(outstanding, remaining_to_allocate)
        contra = impairment.accumulated_impairment_account
        if not contra.is_active or contra.is_group or not contra.allow_posting or contra.nature != ChartOfAccount.Nature.ASSET:
            raise ValidationError("حساب مجمع الانخفاض المرتبط بأحد الاختبارات السابقة غير صالح للترحيل؛ راجع دليل الحسابات.")
        allocations.append((impairment, allocated))
        debit_by_account[contra.pk] = (contra, debit_by_account.get(contra.pk, (contra, Decimal("0")))[1] + allocated)
        remaining_to_allocate -= allocated
        if remaining_to_allocate <= 0:
            break
    if remaining_to_allocate > Decimal("0.0000"):
        raise ValidationError("رصيد خسائر الانخفاض المتاح للعكس لا يطابق القيمة المحسوبة؛ أوقف الترحيل وراجع سجل العكوس.")

    lines = [
        {"account": account, "debit": amount, "credit": 0, "description": f"عكس مجمع انخفاض الأصل {asset.name_ar}"}
        for account, amount in debit_by_account.values()
    ]
    lines.append({
        "account": income_account, "debit": 0, "credit": reversal_amount,
        "description": f"عكس خسارة انخفاض قيمة الأصل وفق IAS 36: {asset.name_ar}",
    })
    entry = _post_entry(
        asset, year, f"FA-IMPR-{asset.pk}-{reversal_date:%Y%m%d}", reversal_date,
        f"عكس انخفاض قيمة الأصل وفق IAS 36: {asset.name_ar}", lines, user,
    )
    reversal = AssetImpairmentReversal(
        asset=asset, reversal_date=reversal_date,
        carrying_amount_before=carrying_amount, recoverable_amount=recoverable_amount,
        hypothetical_carrying_amount=hypothetical_carrying, reversal_amount=reversal_amount,
        reversal_income_account=income_account, journal_entry=entry,
    )
    reversal.full_clean()
    reversal.save()
    for impairment, allocated in allocations:
        allocation = AssetImpairmentReversalAllocation(
            reversal=reversal, impairment=impairment, amount=allocated,
        )
        allocation.full_clean()
        allocation.save()
    return reversal




@transaction.atomic
def dispose_asset(asset_id, disposal_date, proceeds, cash_account_id, fiscal_year_id, user=None):
    """إثبات بيع الأصل أو استبعاده ومعالجة النقدية ومجمع الإهلاك والربح أو الخسارة."""
    from treasury.models import TreasuryAccount, TreasuryTransaction

    if isinstance(disposal_date, str):
        disposal_date = dateparse.parse_date(disposal_date)
    if not disposal_date:
        raise ValidationError("أدخل تاريخ استبعاد صحيحًا.")
    try:
        proceeds = Decimal(str(proceeds))
    except Exception as exc:
        raise ValidationError("قيمة البيع غير صحيحة.") from exc
    if proceeds < 0:
        raise ValidationError("قيمة البيع لا يمكن أن تكون سالبة.")

    asset = FixedAsset.objects.select_for_update(of=("self",)).select_related(
        "company", "branch", "asset_account", "accumulated_depreciation_account",
        "depreciation_expense_account", "disposal_gain_loss_account", "disposal_loss_account"
    ).get(pk=asset_id)
    if asset.status != FixedAsset.Status.ACTIVE or not asset.acquisition_journal_id:
        raise ValidationError("الأصل غير نشط أو لم يتم إثبات اقتنائه.")
    proceeds = round_currency_amount(proceeds, asset.company.base_currency)
    if disposal_date < asset.depreciation_start_date:
        raise ValidationError("تاريخ الاستبعاد يسبق بداية الإهلاك.")
    if AssetDepreciation.objects.filter(
        asset=asset, period_date__gt=disposal_date, journal_entry__status=JournalEntry.Status.POSTED,
    ).exists():
        raise ValidationError("توجد قيود إهلاك بتاريخ لاحق لتاريخ الاستبعاد؛ راجعها أولًا.")
    if asset.impairments.filter(
        impairment_date__gt=disposal_date, journal_entry__status=JournalEntry.Status.POSTED,
    ).exists() or asset.impairment_reversals.filter(
        reversal_date__gt=disposal_date, journal_entry__status=JournalEntry.Status.POSTED,
    ).exists():
        raise ValidationError("توجد اختبارات انخفاض أو عكوس بتاريخ لاحق لتاريخ الاستبعاد؛ راجع تسلسل الحركات أولًا.")

    try:
        year = FiscalYear.objects.get(pk=fiscal_year_id, company=asset.company, status=FiscalYear.Status.OPEN)
    except (FiscalYear.DoesNotExist, TypeError, ValueError):
        raise ValidationError("حدد سنة مالية مفتوحة وصحيحة للشركة.") from None
    if not (year.start_date <= disposal_date <= year.end_date):
        raise ValidationError("تاريخ الاستبعاد خارج السنة المالية المفتوحة.")

    treasury_account = None
    if proceeds > 0:
        try:
            treasury_account = TreasuryAccount.objects.select_related("account").get(pk=cash_account_id, company=asset.company, is_active=True)
        except (TreasuryAccount.DoesNotExist, TypeError, ValueError):
            raise ValidationError("اختر حساب بنك أو خزينة نشطًا لمتحصلات البيع.") from None
        cash_account = treasury_account.account
        if not cash_account.is_active or cash_account.is_group or not cash_account.allow_posting or cash_account.nature != "asset":
            raise ValidationError("الحساب المحاسبي المرتبط بالخزينة غير صالح.")
        if treasury_account.branch_id and treasury_account.branch_id != asset.branch_id:
            raise ValidationError("الخزينة المختارة لا تتبع فرع الأصل.")

    last_depreciation = asset.depreciations.filter(
        journal_entry__status=JournalEntry.Status.POSTED, period_date__lte=disposal_date,
    ).order_by("-period_date", "-pk").first()
    latest_impairment = asset.impairments.filter(
        journal_entry__status=JournalEntry.Status.POSTED, impairment_date__lte=disposal_date,
    ).order_by("-impairment_date", "-pk").first()
    latest_reversal = asset.impairment_reversals.filter(
        journal_entry__status=JournalEntry.Status.POSTED, reversal_date__lte=disposal_date,
    ).order_by("-reversal_date", "-pk").first()
    latest_measurement_date = max(
        latest_impairment.impairment_date if latest_impairment else asset.depreciation_start_date - timedelta(days=1),
        latest_reversal.reversal_date if latest_reversal else asset.depreciation_start_date - timedelta(days=1),
    )
    previous_date = max(
        last_depreciation.period_date if last_depreciation else asset.depreciation_start_date - timedelta(days=1),
        latest_measurement_date,
    )
    elapsed_days = max(0, (disposal_date - previous_date).days)
    posted_total = asset.depreciations.filter(
        journal_entry__status=JournalEntry.Status.POSTED, period_date__lte=disposal_date,
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0")
    impairment_total = asset.impairments.filter(
        journal_entry__status=JournalEntry.Status.POSTED, impairment_date__lte=disposal_date,
    ).aggregate(total=Sum("impairment_loss"))["total"] or Decimal("0")
    reversal_total = asset.impairment_reversals.filter(
        journal_entry__status=JournalEntry.Status.POSTED, reversal_date__lte=disposal_date,
    ).aggregate(total=Sum("reversal_amount"))["total"] or Decimal("0")
    net_impairment = impairment_total - reversal_total
    remaining = max(Decimal("0"), asset.original_cost - posted_total - net_impairment - asset.salvage_value)

    if elapsed_days and remaining:
        if latest_measurement_date >= asset.depreciation_start_date:
            dep_at_measurement = asset.depreciations.filter(
                journal_entry__status=JournalEntry.Status.POSTED, period_date__lte=latest_measurement_date,
            ).aggregate(total=Sum("amount"))["total"] or Decimal("0")
            loss_at_measurement = asset.impairments.filter(
                journal_entry__status=JournalEntry.Status.POSTED, impairment_date__lte=latest_measurement_date,
            ).aggregate(total=Sum("impairment_loss"))["total"] or Decimal("0")
            reversal_at_measurement = asset.impairment_reversals.filter(
                journal_entry__status=JournalEntry.Status.POSTED, reversal_date__lte=latest_measurement_date,
            ).aggregate(total=Sum("reversal_amount"))["total"] or Decimal("0")
            carrying_at_measurement = asset.original_cost - dep_at_measurement - loss_at_measurement + reversal_at_measurement
            life_used_days = max(0, (latest_measurement_date - asset.depreciation_start_date).days + 1)
            remaining_life_days = max(1, asset.useful_life_months * 30 - life_used_days)
            revised_basis = max(Decimal("0"), carrying_at_measurement - asset.salvage_value)
            daily_rate = revised_basis / Decimal(remaining_life_days)
        else:
            daily_rate = asset.depreciable_amount / Decimal(asset.useful_life_months * 30)
        partial_amount = min(remaining, round_currency_amount(
            daily_rate * Decimal(elapsed_days), asset.company.base_currency,
        ))
        if partial_amount > 0:
            partial_entry = _post_entry(
                asset, year, f"FA-DEP-PART-{asset.pk}-{disposal_date:%Y%m%d}", disposal_date,
                f"إهلاك حتى تاريخ استبعاد الأصل: {asset.name_ar}",
                [
                    {"account": asset.depreciation_expense_account, "debit": partial_amount, "credit": 0, "description": asset.name_ar},
                    {"account": asset.accumulated_depreciation_account, "debit": 0, "credit": partial_amount, "description": asset.name_ar},
                ], user,
            )
            AssetDepreciation.objects.create(
                asset=asset, period_date=disposal_date, amount=partial_amount,
                is_partial_period=True, journal_entry=partial_entry,
            )

    accumulated = asset.depreciations.filter(
        journal_entry__status=JournalEntry.Status.POSTED, period_date__lte=disposal_date,
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0")
    accumulated = min(accumulated, asset.depreciable_amount)
    impairment_total = asset.impairments.filter(
        journal_entry__status=JournalEntry.Status.POSTED, impairment_date__lte=disposal_date,
    ).aggregate(total=Sum("impairment_loss"))["total"] or Decimal("0")
    reversal_total = asset.impairment_reversals.filter(
        journal_entry__status=JournalEntry.Status.POSTED, reversal_date__lte=disposal_date,
    ).aggregate(total=Sum("reversal_amount"))["total"] or Decimal("0")
    net_impairment = impairment_total - reversal_total
    net_book_value = asset.original_cost - accumulated - net_impairment
    gain = max(Decimal("0"), proceeds - net_book_value)
    loss = max(Decimal("0"), net_book_value - proceeds)
    gain_account = asset.disposal_gain_loss_account
    loss_account = asset.disposal_loss_account
    if gain > 0 and gain_account.nature != "revenue":
        raise ValidationError("حساب أرباح الاستبعاد يجب أن يكون من طبيعة الإيرادات.")
    if loss > 0:
        if loss_account is None and gain_account.nature == "expense":
            loss_account = gain_account
        if loss_account is None or loss_account.nature != "expense":
            raise ValidationError("حدد حساب خسائر الاستبعاد من طبيعة المصروفات.")
    settlement_counter = gain_account if gain > 0 else loss_account if loss > 0 else asset.asset_account

    lines = [
        {"account": asset.accumulated_depreciation_account, "debit": accumulated, "credit": 0, "description": f"إقفال مجمع إهلاك {asset.name_ar}"},
    ]
    impairment_account_balances = {}
    impairment_rows = asset.impairments.filter(
        journal_entry__status=JournalEntry.Status.POSTED, impairment_date__lte=disposal_date,
    ).select_related("accumulated_impairment_account")
    for impairment in impairment_rows:
        reversed_amount = impairment.reversal_allocations.filter(
            reversal__journal_entry__status=JournalEntry.Status.POSTED,
            reversal__reversal_date__lte=disposal_date,
        ).aggregate(total=Sum("amount"))["total"] or Decimal("0")
        outstanding = impairment.impairment_loss - reversed_amount
        if outstanding > 0:
            account_id = impairment.accumulated_impairment_account_id
            impairment_account_balances[account_id] = impairment_account_balances.get(account_id, Decimal("0")) + outstanding
    for account_id, balance in impairment_account_balances.items():
        lines.append({
            "account_id": account_id, "debit": balance, "credit": 0,
            "description": f"إقفال صافي مجمع انخفاض قيمة {asset.name_ar}",
        })
    lines.append({"account": asset.asset_account, "debit": 0, "credit": asset.original_cost, "description": f"استبعاد تكلفة {asset.name_ar}"})
    if proceeds > 0:
        lines.insert(0, {"account": cash_account, "debit": proceeds, "credit": 0, "description": f"متحصلات استبعاد {asset.name_ar}"})
    if gain > 0:
        lines.append({"account": gain_account, "debit": 0, "credit": gain, "description": f"ربح استبعاد {asset.name_ar}"})
    elif loss > 0:
        lines.append({"account": loss_account, "debit": loss, "credit": 0, "description": f"خسارة استبعاد {asset.name_ar}"})

    entry = _post_entry(
        asset, year, f"FA-DISP-{asset.pk}", disposal_date,
        f"بيع / استبعاد الأصل: {asset.name_ar}", lines, user,
    )
    treasury_doc = None
    if proceeds > 0:
        treasury_doc = TreasuryTransaction.objects.create(
            company=asset.company, branch=asset.branch, fiscal_year=year, source_account=treasury_account,
            counter_account=settlement_counter, kind=TreasuryTransaction.Kind.RECEIPT,
            status=TreasuryTransaction.Status.POSTED, document_number=f"FA-DISP-{asset.pk}",
            transaction_date=disposal_date, amount=proceeds,
            description=f"متحصلات بيع / استبعاد الأصل: {asset.name_ar}",
            cash_flow_category=TreasuryTransaction.CashFlowCategory.INVESTING,
            journal_entry=entry, created_by=user, posted_at=timezone.now(),
        )
    asset.status = FixedAsset.Status.DISPOSED
    asset.disposed_at = disposal_date
    asset.disposal_proceeds = proceeds
    asset.disposal_journal = entry
    asset.disposal_treasury_transaction = treasury_doc
    asset.save(update_fields=("status", "disposed_at", "disposal_proceeds", "disposal_journal", "disposal_treasury_transaction"))
    return {
        "asset_id": asset.pk, "disposal_journal": entry.pk, "treasury_transaction": treasury_doc.pk if treasury_doc else None, "accumulated_depreciation": accumulated,
        "net_book_value": net_book_value, "proceeds": proceeds, "gain": gain, "loss": loss,
    }


@transaction.atomic
def post_asset_purchase_invoice(invoice_id, user=None):
    """ترحيل فاتورة شراء أصل مباشر: تكلفة الأصل والضريبة القابلة للاسترداد مقابل حساب الموردين."""
    invoice = AssetPurchaseInvoice.objects.select_for_update(of=("self",)).select_related(
        "company", "branch", "asset", "supplier", "fiscal_year", "tax_code",
    ).get(pk=invoice_id)
    if invoice.status != AssetPurchaseInvoice.Status.DRAFT:
        raise ValidationError("فاتورة شراء الأصل مرحلة بالفعل.")
    from approvals.models import FinancialApprovalRequest
    from approvals.services import require_financial_approval
    try:
        require_financial_approval(FinancialApprovalRequest.DocumentType.ASSET_PURCHASE_INVOICE, invoice)
    except DjangoValidationError as exc:
        raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages) from exc
    if invoice.fiscal_year.status != FiscalYear.Status.OPEN or not (
        invoice.fiscal_year.start_date <= invoice.invoice_date <= invoice.fiscal_year.end_date
    ):
        raise ValidationError("تاريخ الفاتورة يجب أن يقع داخل سنة مالية مفتوحة.")
    if not invoice.supplier.is_active:
        raise ValidationError("لا يمكن ترحيل فاتورة مورد موقوف.")
    asset = FixedAsset.objects.select_for_update(of=("self",)).get(pk=invoice.asset_id)
    if asset.acquisition_journal_id or asset.status != FixedAsset.Status.ACTIVE:
        raise ValidationError("الأصل مثبت محاسبيًا أو مستبعد بالفعل.")
    if asset.company_id != invoice.company_id or asset.branch_id != invoice.branch_id:
        raise ValidationError("بيانات الأصل لا تطابق فاتورة المورد.")
    expected_cost = invoice.net_amount + invoice.nonrecoverable_tax_amount
    if asset.salvage_value > expected_cost:
        raise ValidationError("القيمة التخريدية تتجاوز تكلفة الأصل بعد معالجة الضريبة.")
    if invoice.tax_code_id:
        code = invoice.tax_code
        if (
            not code.is_active or code.company_id != invoice.company_id
            or code.country != invoice.company.country
            or code.category == "reverse_charge"
            or code.effective_from > invoice.invoice_date
            or (code.effective_to and code.effective_to < invoice.invoice_date)
        ):
            raise ValidationError("كود الضريبة غير صالح لهذه الشركة أو تاريخ الفاتورة.")
        expected_tax = round_currency_amount(
            invoice.net_amount * code.rate / Decimal("100"), invoice.company.base_currency,
        )
        if invoice.tax_amount != expected_tax:
            raise ValidationError("قيمة الضريبة لا تطابق نسبة كود الضريبة وصافي الفاتورة.")
        expected_recoverable = invoice.tax_amount if code.input_tax_recoverable else Decimal("0")
        expected_nonrecoverable = invoice.tax_amount - expected_recoverable
        if (
            invoice.recoverable_tax_amount != expected_recoverable
            or invoice.nonrecoverable_tax_amount != expected_nonrecoverable
        ):
            raise ValidationError("توزيع الضريبة لا يطابق قابلية الاسترداد في كود الضريبة.")
    elif invoice.tax_amount or invoice.recoverable_tax_amount or invoice.nonrecoverable_tax_amount:
        raise ValidationError("لا يمكن تسجيل ضريبة دون كود ضريبي معتمد.")

    payable = get_account_for_role(invoice.company, AccountRole.Role.ACCOUNTS_PAYABLE)
    vat_input = (
        get_account_for_role(invoice.company, AccountRole.Role.VAT_INPUT)
        if invoice.recoverable_tax_amount > 0 else None
    )
    accounts = [asset.asset_account, payable] + ([vat_input] if vat_input else [])
    if len({account.pk for account in accounts}) != len(accounts):
        raise ValidationError("يجب فصل حساب الأصل والموردين وضريبة المدخلات.")
    if payable.nature != "liability" or not payable.is_active or payable.is_group or not payable.allow_posting:
        raise ValidationError("حساب الموردين المرتبط غير صالح.")
    if vat_input and (vat_input.nature != "asset" or not vat_input.is_active or vat_input.is_group or not vat_input.allow_posting):
        raise ValidationError("حساب ضريبة المدخلات المرتبط غير صالح.")

    entry = JournalEntry.objects.create(
        company=invoice.company, branch=invoice.branch, fiscal_year=invoice.fiscal_year,
        entry_number=f"FA-INV-{invoice.pk}", entry_date=invoice.invoice_date,
        description=f"فاتورة شراء أصل من {invoice.supplier.name_ar} رقم {invoice.invoice_number}"[:500],
        source_type=JournalEntry.Source.ASSETS, created_by=user,
    )
    lines = [
        JournalLine(
            entry=entry, account=asset.asset_account,
            description=f"رسملة تكلفة الأصل {asset.name_ar}",
            debit=expected_cost, credit=0,
        ),
        JournalLine(
            entry=entry, account=payable,
            description=f"إثبات مستحق المورد {invoice.supplier.name_ar}",
            debit=0, credit=invoice.grand_total,
        ),
    ]
    if vat_input and invoice.recoverable_tax_amount:
        lines.append(JournalLine(
            entry=entry, account=vat_input,
            description="ضريبة مدخلات قابلة للاسترداد",
            debit=invoice.recoverable_tax_amount, credit=0,
        ))
    JournalLine.objects.bulk_create(lines)
    try:
        posted = post_journal(entry.pk, actor=user)
    except DjangoValidationError as exc:
        raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages) from exc

    asset.original_cost = expected_cost
    asset.acquisition_net_amount = invoice.net_amount
    asset.nonrecoverable_tax_amount = invoice.nonrecoverable_tax_amount
    asset.acquisition_tax_code = invoice.tax_code
    asset.supplier_name = invoice.supplier.name_ar
    asset.supplier_invoice = invoice.invoice_number
    asset.acquisition_journal = posted
    asset.save(update_fields=(
        "original_cost", "acquisition_net_amount", "nonrecoverable_tax_amount",
        "acquisition_tax_code", "supplier_name", "supplier_invoice", "acquisition_journal",
    ))
    invoice.status = AssetPurchaseInvoice.Status.POSTED
    invoice.journal_entry = posted
    invoice.posted_at = timezone.now()
    invoice.save(update_fields=("status", "journal_entry", "posted_at"))
    return invoice


@transaction.atomic
def post_asset_supplier_payment(payment_id, user=None):
    """ترحيل سداد فاتورة الأصل على حساب الموردين مع منع تجاوز الرصيد المستحق."""
    payment = AssetSupplierPayment.objects.select_for_update(of=("self",)).select_related(
        "company", "branch", "invoice", "supplier", "payment_account",
    ).get(pk=payment_id)
    if payment.status != AssetSupplierPayment.Status.DRAFT:
        raise ValidationError("سند سداد فاتورة الأصل ليس مسودة.")
    from approvals.models import FinancialApprovalRequest
    from approvals.services import require_financial_approval
    try:
        require_financial_approval(FinancialApprovalRequest.DocumentType.ASSET_SUPPLIER_PAYMENT, payment)
    except DjangoValidationError as exc:
        raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages) from exc
    invoice = AssetPurchaseInvoice.objects.select_for_update(of=("self",)).get(pk=payment.invoice_id)
    if invoice.status != AssetPurchaseInvoice.Status.POSTED:
        raise ValidationError("لا يمكن سداد فاتورة أصل غير مرحلة.")
    if payment.company_id != invoice.company_id or payment.branch_id != invoice.branch_id or payment.supplier_id != invoice.supplier_id:
        raise ValidationError("بيانات سند السداد لا تطابق فاتورة الأصل.")
    if payment.payment_date < invoice.invoice_date:
        raise ValidationError("تاريخ السداد لا يمكن أن يسبق تاريخ الفاتورة.")
    paid = AssetSupplierPayment.objects.filter(
        invoice=invoice, status=AssetSupplierPayment.Status.POSTED,
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0")
    remaining = invoice.grand_total - paid
    if payment.amount > remaining:
        raise ValidationError(f"قيمة السداد تتجاوز المتبقي على الفاتورة ({remaining}).")
    year = FiscalYear.objects.filter(
        company=payment.company, status=FiscalYear.Status.OPEN,
        start_date__lte=payment.payment_date, end_date__gte=payment.payment_date,
    ).order_by("-start_date").first()
    if not year:
        raise ValidationError("لا توجد سنة مالية مفتوحة تشمل تاريخ السداد.")
    payable = get_account_for_role(payment.company, AccountRole.Role.ACCOUNTS_PAYABLE)
    cash = payment.payment_account
    if cash.company_id != payment.company_id or cash.nature != "asset" or not cash.is_active or cash.is_group or not cash.allow_posting:
        raise ValidationError("حساب البنك أو الخزينة غير صالح.")
    if cash.pk == payable.pk:
        raise ValidationError("يجب فصل حساب الموردين عن حساب البنك أو الخزينة.")

    entry = JournalEntry.objects.create(
        company=payment.company, branch=payment.branch, fiscal_year=year,
        entry_number=f"FA-PAY-{payment.pk}", entry_date=payment.payment_date,
        description=f"سداد فاتورة أصل {invoice.invoice_number} للمورد {payment.supplier.name_ar}"[:500],
        source_type=JournalEntry.Source.ASSETS, created_by=user,
    )
    JournalLine.objects.bulk_create([
        JournalLine(entry=entry, account=payable, description="تخفيض مستحق المورد", debit=payment.amount, credit=0),
        JournalLine(entry=entry, account=cash, description="سداد من البنك أو الخزينة", debit=0, credit=payment.amount),
    ])
    try:
        posted = post_journal(entry.pk, actor=user)
    except DjangoValidationError as exc:
        raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages) from exc

    # Keep bank/cash movements visible in the treasury cash-flow subsystem as well as the GL.
    from treasury.models import TreasuryAccount, TreasuryTransaction
    from treasury.services import record_posted_document_bank_movement
    treasury_account = TreasuryAccount.objects.filter(
        company=payment.company, account=cash, kind=TreasuryAccount.Kind.BANK, is_active=True,
    ).first()
    if (
        not treasury_account
        or (treasury_account.branch_id and treasury_account.branch_id != payment.branch_id)
        or treasury_account.currency_code != payment.company.base_currency.code
    ):
        raise ValidationError("حساب السداد يجب أن يكون مرتبطًا بحساب خزينة/بنك نشط بعملة الشركة ومطابقًا للفرع.")
    record_posted_document_bank_movement(
        company=payment.company, branch=payment.branch, fiscal_year=year, account=cash,
        kind=TreasuryTransaction.Kind.PAYMENT, amount=payment.amount,
        transaction_date=payment.payment_date, document_number=f"AUTO-FA-PAY-{payment.pk}",
        description=f"سداد فاتورة أصل {invoice.invoice_number} - سند {payment.payment_number}",
        counter_account=payable, journal_entry=posted, user=user, reference=payment.reference,
        cash_flow_category=TreasuryTransaction.CashFlowCategory.INVESTING,
    )
    payment.journal_entry = posted
    payment.status = AssetSupplierPayment.Status.POSTED
    payment.posted_at = timezone.now()
    payment.save(update_fields=("journal_entry", "status", "posted_at"))
    return payment
