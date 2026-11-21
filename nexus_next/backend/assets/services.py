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
    if AssetDepreciation.objects.filter(asset=asset, period_date=period_date).exists():
        raise ValidationError("تم تسجيل إهلاك هذا الأصل لهذه الفترة من قبل.")
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
