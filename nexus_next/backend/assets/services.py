from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone, dateparse
from django.db.models import Sum

from accounting.models import JournalEntry, JournalLine
from accounting.services import post_journal
from companies.models import FiscalYear
from .models import FixedAsset, AssetDepreciation


MONEY = Decimal("0.0001")


def _post_entry(asset, fiscal_year, entry_number, entry_date, description, lines, user=None):
    entry = JournalEntry.objects.create(
        company=asset.company, branch=asset.branch, fiscal_year=fiscal_year,
        entry_number=entry_number, entry_date=entry_date, description=description,
        source_type=JournalEntry.Source.ASSETS, created_by=user,
    )
    JournalLine.objects.bulk_create([JournalLine(entry=entry, **line) for line in lines])
    return post_journal(entry.pk)


@transaction.atomic
def acquire_asset(asset_id, fiscal_year_id, user=None):
    asset = FixedAsset.objects.select_for_update().get(pk=asset_id)
    if asset.acquisition_journal_id:
        raise ValidationError("تم تسجيل قيد اقتناء الأصل بالفعل.")
    try:
        year = FiscalYear.objects.get(pk=fiscal_year_id, company=asset.company, status=FiscalYear.Status.OPEN)
    except (FiscalYear.DoesNotExist, TypeError, ValueError):
        raise ValidationError("حدد سنة مالية مفتوحة وصحيحة للشركة.")
    if not (year.start_date <= asset.acquisition_date <= year.end_date):
        raise ValidationError("تاريخ الاقتناء خارج السنة المالية المفتوحة.")
    entry = _post_entry(asset, year, f"FA-ACQ-{asset.pk}", asset.acquisition_date,
        f"إثبات اقتناء الأصل: {asset.name_ar}", [
            {"account": asset.asset_account, "debit": asset.original_cost, "credit": 0, "description": asset.name_ar},
            {"account": asset.acquisition_counter_account, "debit": 0, "credit": asset.original_cost, "description": asset.name_ar},
        ], user)
    from treasury.models import TreasuryAccount, TreasuryTransaction
    treasury_account = TreasuryAccount.objects.filter(
        company=asset.company, account=asset.acquisition_counter_account, is_active=True
    ).first()
    treasury_doc = None
    if treasury_account:
        if treasury_account.branch_id and treasury_account.branch_id != asset.branch_id:
            raise ValidationError("حساب الخزينة المرتبط بمقابل الاقتناء لا يتبع فرع الأصل.")
        treasury_doc = TreasuryTransaction.objects.create(
            company=asset.company, branch=asset.branch, fiscal_year=year,
            source_account=treasury_account, counter_account=asset.asset_account,
            kind=TreasuryTransaction.Kind.PAYMENT, status=TreasuryTransaction.Status.POSTED,
            document_number=f"FA-ACQ-{asset.pk}", transaction_date=asset.acquisition_date,
            amount=asset.original_cost, description=f"سداد تكلفة اقتناء الأصل: {asset.name_ar}",
            cash_flow_category=TreasuryTransaction.CashFlowCategory.INVESTING,
            journal_entry=entry, created_by=user, posted_at=timezone.now(),
        )
    asset.acquisition_journal = entry
    asset.acquisition_treasury_transaction = treasury_doc
    asset.save(update_fields=("acquisition_journal", "acquisition_treasury_transaction"))
    return asset


@transaction.atomic
def depreciate_asset(asset_id, period_date, fiscal_year_id, user=None):
    if isinstance(period_date, str):
        period_date = dateparse.parse_date(period_date)
    if not period_date:
        raise ValidationError("أدخل تاريخ فترة إهلاك صحيحًا.")
    asset = FixedAsset.objects.select_for_update().get(pk=asset_id)
    if asset.status != FixedAsset.Status.ACTIVE or not asset.acquisition_journal_id:
        raise ValidationError("الأصل غير نشط أو لم يتم تسجيل اقتنائه.")
    if period_date < asset.depreciation_start_date:
        raise ValidationError("تاريخ الإهلاك يسبق بداية الإهلاك المحددة.")
    if AssetDepreciation.objects.filter(asset=asset, period_date__year=period_date.year, period_date__month=period_date.month).exists():
        raise ValidationError("تم تسجيل إهلاك هذا الأصل لهذا الشهر من قبل.")
    try:
        year = FiscalYear.objects.get(pk=fiscal_year_id, company=asset.company, status=FiscalYear.Status.OPEN)
    except (FiscalYear.DoesNotExist, TypeError, ValueError):
        raise ValidationError("حدد سنة مالية مفتوحة وصحيحة للشركة.")
    if not (year.start_date <= period_date <= year.end_date):
        raise ValidationError("تاريخ الإهلاك خارج السنة المالية المفتوحة.")
    last_depreciation = asset.depreciations.order_by("-period_date", "-pk").first()
    previous_date = last_depreciation.period_date if last_depreciation else asset.depreciation_start_date - timedelta(days=1)
    elapsed_days = (period_date - previous_date).days
    if elapsed_days <= 0:
        raise ValidationError("تاريخ الإهلاك يجب أن يكون بعد آخر فترة إهلاك مسجلة.")
    posted_total = asset.depreciations.aggregate(total=Sum("amount"))["total"] or Decimal("0")
    remaining = asset.depreciable_amount - posted_total
    if remaining <= 0:
        raise ValidationError("تم استهلاك كامل القيمة القابلة للإهلاك.")
    daily_rate = asset.depreciable_amount / Decimal(asset.useful_life_months * 30)
    amount = min((daily_rate * Decimal(elapsed_days)).quantize(MONEY, rounding=ROUND_HALF_UP), remaining)
    entry = _post_entry(asset, year, f"FA-DEP-{asset.pk}-{period_date:%Y%m}", period_date,
        f"إهلاك الأصل: {asset.name_ar} - {period_date:%Y-%m}", [
            {"account": asset.depreciation_expense_account, "debit": amount, "credit": 0, "description": asset.name_ar},
            {"account": asset.accumulated_depreciation_account, "debit": 0, "credit": amount, "description": asset.name_ar},
        ], user)
    AssetDepreciation.objects.create(asset=asset, period_date=period_date, amount=amount, journal_entry=entry)
    return amount


@transaction.atomic
def dispose_asset(asset_id, disposal_date, proceeds, cash_account_id, fiscal_year_id, user=None):
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

    asset = FixedAsset.objects.select_for_update().select_related(
        "company", "branch", "asset_account", "accumulated_depreciation_account",
        "depreciation_expense_account", "disposal_gain_loss_account", "disposal_loss_account"
    ).get(pk=asset_id)
    if asset.status != FixedAsset.Status.ACTIVE or not asset.acquisition_journal_id:
        raise ValidationError("الأصل غير نشط أو لم يتم إثبات اقتنائه.")
    if disposal_date < asset.depreciation_start_date:
        raise ValidationError("تاريخ الاستبعاد يسبق بداية الإهلاك.")
    if AssetDepreciation.objects.filter(asset=asset, period_date__gt=disposal_date).exists():
        raise ValidationError("توجد قيود إهلاك بتاريخ لاحق لتاريخ الاستبعاد؛ راجعها أولًا.")

    try:
        year = FiscalYear.objects.get(pk=fiscal_year_id, company=asset.company, status=FiscalYear.Status.OPEN)
    except (FiscalYear.DoesNotExist, TypeError, ValueError):
        raise ValidationError("حدد سنة مالية مفتوحة وصحيحة للشركة.")
    if not (year.start_date <= disposal_date <= year.end_date):
        raise ValidationError("تاريخ الاستبعاد خارج السنة المالية المفتوحة.")

    treasury_account = None
    if proceeds > 0:
        try:
            treasury_account = TreasuryAccount.objects.select_related("account").get(pk=cash_account_id, company=asset.company, is_active=True)
        except (TreasuryAccount.DoesNotExist, TypeError, ValueError):
            raise ValidationError("اختر حساب بنك أو خزينة نشطًا لمتحصلات البيع.")
        cash_account = treasury_account.account
        if not cash_account.is_active or cash_account.is_group or not cash_account.allow_posting or cash_account.nature != "asset":
            raise ValidationError("الحساب المحاسبي المرتبط بالخزينة غير صالح.")
        if treasury_account.branch_id and treasury_account.branch_id != asset.branch_id:
            raise ValidationError("الخزينة المختارة لا تتبع فرع الأصل.")

    last_depreciation = asset.depreciations.order_by("-period_date", "-pk").first()
    previous_date = last_depreciation.period_date if last_depreciation else asset.depreciation_start_date - timedelta(days=1)
    elapsed_days = max(0, (disposal_date - previous_date).days)
    posted_total = asset.depreciations.aggregate(total=Sum("amount"))["total"] or Decimal("0")
    remaining = max(Decimal("0"), asset.depreciable_amount - posted_total)

    if elapsed_days and remaining:
        daily_rate = asset.depreciable_amount / Decimal(asset.useful_life_months * 30)
        partial_amount = min(remaining, (daily_rate * Decimal(elapsed_days)).quantize(MONEY, rounding=ROUND_HALF_UP))
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

    accumulated = asset.depreciations.aggregate(total=Sum("amount"))["total"] or Decimal("0")
    accumulated = min(accumulated, asset.depreciable_amount)
    net_book_value = asset.original_cost - accumulated
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
        {"account": asset.asset_account, "debit": 0, "credit": asset.original_cost, "description": f"استبعاد تكلفة {asset.name_ar}"},
    ]
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
