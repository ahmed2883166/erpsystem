"""Strict Decimal parsing for company configuration values."""
from decimal import Decimal, InvalidOperation


def parse_decimal_setting(value, *, minimum=None, maximum=None):
    """Parse a finite decimal value and enforce optional inclusive bounds."""
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("A valid decimal number is required.") from None

    if not number.is_finite():
        raise ValueError("A finite decimal number is required.")

    if minimum is not None and number < Decimal(str(minimum)):
        raise ValueError("The decimal number is below the allowed minimum.")
    if maximum is not None and number > Decimal(str(maximum)):
        raise ValueError("The decimal number is above the allowed maximum.")
    return number
