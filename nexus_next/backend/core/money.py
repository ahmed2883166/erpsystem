"""Currency-aware rounding helpers for monetary amounts."""
from decimal import Decimal, ROUND_HALF_UP


def round_currency_amount(amount, currency):
    """Round an amount to the configured minor-unit precision of its currency.

    Persisted invoice monetary fields remain DECIMAL(20,4); the currency's
    decimal_places controls the monetary rounding point, not storage scale.
    """
    places = getattr(currency, "decimal_places", 2)
    try:
        places = int(places)
    except (TypeError, ValueError):
        places = 2
    if places < 0 or places > 8:
        raise ValueError("Currency decimal_places must be between 0 and 8.")
    quantum = Decimal("1").scaleb(-places)
    return Decimal(amount).quantize(quantum, rounding=ROUND_HALF_UP)
