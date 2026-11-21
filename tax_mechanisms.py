"""Independent tax-mechanism calculations for Egypt and Saudi Arabia.

This module deliberately does not post journals or mutate invoices. Callers must
supply validated tax rules and map the returned amounts to configured account roles.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


CENT = Decimal("0.01")


def _decimal(value, field_name):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{field_name} يجب أن يكون رقمًا صحيحًا.") from None
    if not result.is_finite():
        raise ValueError(f"{field_name} يجب أن يكون رقمًا محدودًا.")
    return result


def _money(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def calculate_egypt_table_tax(tax_type, taxable_base, *, rate=None,
                              quantity=None, amount_per_unit=None):
    """Calculate ETA T2 percentage or T3 fixed-amount table tax separately from VAT.

    T2 uses taxable_base * rate / 100. T3 uses quantity * amount_per_unit.
    The result is a separate tax amount and is never folded into standard VAT.
    """
    tax_type = str(tax_type or "").strip().upper()
    if tax_type not in {"T2", "T3"}:
        raise ValueError("نوع ضريبة الجدول يجب أن يكون T2 نسبيًا أو T3 نوعيًا.")
    base = _decimal(taxable_base, "الوعاء")
    if base < 0:
        raise ValueError("وعاء ضريبة الجدول لا يمكن أن يكون سالبًا.")

    if tax_type == "T2":
        percentage = _decimal(rate, "نسبة ضريبة الجدول")
        if percentage < 0:
            raise ValueError("نسبة ضريبة الجدول لا يمكن أن تكون سالبة.")
        return {
            "tax_type": "T2",
            "taxable_base": _money(base),
            "quantity": None,
            "rate": percentage,
            "amount_per_unit": None,
            "tax_amount": _money(base * percentage / Decimal("100")),
        }

    units = _decimal(quantity, "الكمية")
    unit_amount = _decimal(amount_per_unit, "الضريبة النوعية للوحدة")
    if units < 0 or unit_amount < 0:
        raise ValueError("الكمية والضريبة النوعية للوحدة لا يمكن أن تكونا سالبتين.")
    return {
        "tax_type": "T3",
        "taxable_base": _money(base),
        "quantity": units,
        "rate": None,
        "amount_per_unit": unit_amount,
        "tax_amount": _money(units * unit_amount),
    }


def calculate_saudi_reverse_charge(country, taxable_base, rate,
                                   deductible_percent=100):
    """Calculate Saudi RCM output VAT and recoverable/non-recoverable input VAT.

    Reporting output tax and eligible input-tax deduction are separate values
    in the same tax period. This helper does not determine legal eligibility.
    """
    if str(country or "").strip().upper() != "SA":
        raise ValueError("آلية الاحتساب العكسي متاحة هنا للسعودية فقط.")
    base = _decimal(taxable_base, "وعاء الاحتساب العكسي")
    percentage = _decimal(rate, "نسبة الضريبة")
    deduction = _decimal(deductible_percent, "نسبة خصم ضريبة المدخلات")
    if base < 0 or percentage < 0:
        raise ValueError("الوعاء ونسبة الضريبة لا يمكن أن يكونا سالبين.")
    if not Decimal("0") <= deduction <= Decimal("100"):
        raise ValueError("نسبة خصم ضريبة المدخلات يجب أن تكون بين 0 و100.")

    output_tax = _money(base * percentage / Decimal("100"))
    recoverable = _money(output_tax * deduction / Decimal("100"))
    non_recoverable = output_tax - recoverable
    return {
        "taxable_base": _money(base),
        "rate": percentage,
        "deductible_percent": deduction,
        "output_tax": output_tax,
        "recoverable_input_tax": recoverable,
        "non_recoverable_input_tax": non_recoverable,
        "net_vat_effect": output_tax - recoverable,
    }
