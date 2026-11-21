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
    asset.acquisition_journal = entry
    asset.save(update_fields=("acquisition_journal",))
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
    posted_total = asset.depreciations.aggregate(total=Sum("amount"))["total"] or Decimal("0")
    remaining = asset.depreciable_amount - posted_total
    if remaining <= 0:
        raise ValidationError("تم استهلاك كامل القيمة القابلة للإهلاك.")
    monthly = (asset.depreciable_amount / Decimal(asset.useful_life_months)).quantize(MONEY, rounding=ROUND_HALF_UP)
    amount = min(monthly, remaining)
    entry = _post_entry(asset, year, f"FA-DEP-{asset.pk}-{period_date:%Y%m}", period_date,
        f"إهلاك الأصل: {asset.name_ar} - {period_date:%Y-%m}", [
            {"account": asset.depreciation_expense_account, "debit": amount, "credit": 0, "description": asset.name_ar},
            {"account": asset.accumulated_depreciation_account, "debit": 0, "credit": amount, "description": asset.name_ar},
        ], user)
    AssetDepreciation.objects.create(asset=asset, period_date=period_date, amount=amount, journal_entry=entry)
    return amount


@transaction.atomic
def dispose_asset(asset_id, disposal_date, proceeds, cash_account_id, fiscal_year_id, user=None):
    from datetime import timedelta
    from accounting.models import ChartOfAccount

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
        "depreciation_expense_account", "disposal_gain_loss_account"
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

    try:
        cash_account = ChartOfAccount.objects.get(pk=cash_account_id, company=asset.company)
    except (ChartOfAccount.DoesNotExist, TypeError, ValueError):
        raise ValidationError("حساب البنك أو الخزينة غير موجود في الشركة.")
    if not cash_account.is_active or cash_account.is_group or not cash_account.allow_posting or cash_account.nature != "asset":
        raise ValidationError("اختر حساب بنك أو خزينة نشطًا وتفصيليًا من طبيعة الأصول.")

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
    gain_loss_account = asset.disposal_gain_loss_account
    expected_nature = "revenue" if gain > 0 else "expense" if loss > 0 else None
    if expected_nature and gain_loss_account.nature != expected_nature:
        raise ValidationError("طبيعة حساب أرباح/خسائر الاستبعاد لا تتوافق مع نتيجة البيع؛ راجع ربط الحساب.")

    lines = [
        {"account": cash_account, "debit": proceeds, "credit": 0, "description": f"متحصلات استبعاد {asset.name_ar}"},
        {"account": asset.accumulated_depreciation_account, "debit": accumulated, "credit": 0, "description": f"إقفال مجمع إهلاك {asset.name_ar}"},
        {"account": asset.asset_account, "debit": 0, "credit": asset.original_cost, "description": f"استبعاد تكلفة {asset.name_ar}"},
    ]
    if gain > 0:
        lines.append({"account": gain_loss_account, "debit": 0, "credit": gain, "description": f"ربح استبعاد {asset.name_ar}"})
    elif loss > 0:
        lines.append({"account": gain_loss_account, "debit": loss, "credit": 0, "description": f"خسارة استبعاد {asset.name_ar}"})

    entry = _post_entry(
        asset, year, f"FA-DISP-{asset.pk}", disposal_date,
        f"بيع / استبعاد الأصل: {asset.name_ar}", lines, user,
    )
    asset.status = FixedAsset.Status.DISPOSED
    asset.disposed_at = disposal_date
    asset.disposal_proceeds = proceeds
    asset.disposal_journal = entry
    asset.save(update_fields=("status", "disposed_at", "disposal_proceeds", "disposal_journal"))
    return {
        "asset_id": asset.pk, "disposal_journal": entry.pk, "accumulated_depreciation": accumulated,
        "net_book_value": net_book_value, "proceeds": proceeds, "gain": gain, "loss": loss,
    }
