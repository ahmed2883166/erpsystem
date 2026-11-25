"""خدمات أسعار الصرف التاريخية والتحويل إلى العملة الأساسية."""

from decimal import Decimal

from django.core.exceptions import ValidationError

from .models import ExchangeRate


def get_exchange_rate(*, company, currency, on_date):
    """إرجاع سعر العملة الساري في التاريخ المطلوب دون استخدام سعر مستقبلي."""
    if currency.pk == company.base_currency_id:
        return Decimal("1")
    if not currency.is_active:
        raise ValidationError("العملة المطلوبة موقوفة.")
    record = ExchangeRate.objects.filter(
        company=company,
        currency=currency,
        rate_date__lte=on_date,
    ).order_by("-rate_date", "-pk").first()
    if record is None:
        raise ValidationError(
            f"لا يوجد سعر صرف مسجل لعملة {currency.code} في أو قبل تاريخ {on_date}."
        )
    return record.rate


def convert_to_base(*, company, currency, amount, on_date, rounding=None):
    """تحويل مبلغ بعملة المستند إلى قيمة العملة الأساسية مع إرجاع السعر المستخدم."""
    rate = get_exchange_rate(company=company, currency=currency, on_date=on_date)
    converted = Decimal(amount) * rate
    if rounding is not None:
        converted = converted.quantize(rounding)
    return converted, rate
