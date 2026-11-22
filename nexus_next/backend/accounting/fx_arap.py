"""Deterministic FX rules shared by foreign-currency AR/AP document posting."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

FOUR_PLACES = Decimal("0.0001")
ZERO = Decimal("0.0000")


def decimal_value(value, field):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a valid decimal.") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be finite.")
    return result


def to_base_amount(foreign_amount, rate):
    foreign = decimal_value(foreign_amount, "foreign_amount")
    exchange_rate = decimal_value(rate, "rate")
    if foreign <= 0 or exchange_rate <= 0:
        raise ValueError("Foreign amount and exchange rate must be positive.")
    return (foreign * exchange_rate).quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)


def validate_snapshot(*, base_currency_code, currency_code, foreign_amount, rate,
                      rate_date, document_date, approved_rate):
    """Validate a complete, approved FX snapshot and return its base-currency value."""
    if not all((currency_code, foreign_amount is not None, rate is not None,
                rate_date, document_date, approved_rate is not None)):
        raise ValueError("A complete currency, foreign amount, rate and rate date are required.")
    if currency_code == base_currency_code:
        raise ValueError("The company's base currency must not carry an FX snapshot.")
    if rate_date > document_date:
        raise ValueError("The exchange-rate date cannot be after the document date.")
    exchange_rate = decimal_value(rate, "rate")
    approved = decimal_value(approved_rate, "approved_rate")
    if exchange_rate <= 0 or exchange_rate != approved:
        raise ValueError("The exchange rate must exactly match the company's approved rate for that date.")
    return to_base_amount(foreign_amount, exchange_rate)


@dataclass(frozen=True)
class SettlementResult:
    settlement_base_amount: Decimal
    carrying_base_amount: Decimal
    realized_gain: Decimal
    realized_loss: Decimal
    remaining_foreign_amount: Decimal
    remaining_carrying_base_amount: Decimal


def settle_foreign_balance(*, direction, remaining_foreign_amount,
                           remaining_carrying_base_amount, settled_foreign_amount,
                           settlement_rate):
    """Allocate historical carrying value and compute realized FX for AR or AP.

    The final settlement consumes the exact remaining carrying value, eliminating
    cumulative rounding residue. Returned gain/loss are always non-negative.
    """
    if direction not in {"receivable", "payable"}:
        raise ValueError("direction must be 'receivable' or 'payable'.")
    remaining_foreign = decimal_value(remaining_foreign_amount, "remaining_foreign_amount")
    remaining_base = decimal_value(remaining_carrying_base_amount, "remaining_carrying_base_amount")
    settled_foreign = decimal_value(settled_foreign_amount, "settled_foreign_amount")
    if remaining_foreign <= 0 or remaining_base < 0 or settled_foreign <= 0:
        raise ValueError("Remaining and settlement balances are invalid.")
    if settled_foreign > remaining_foreign:
        raise ValueError("Settlement exceeds the remaining foreign-currency balance.")
    settlement_base = to_base_amount(settled_foreign, settlement_rate)
    if settled_foreign == remaining_foreign:
        carrying = remaining_base.quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)
    else:
        carrying = (remaining_base * settled_foreign / remaining_foreign).quantize(
            FOUR_PLACES, rounding=ROUND_HALF_UP
        )
    difference = settlement_base - carrying
    if direction == "receivable":
        gain, loss = (difference, ZERO) if difference >= 0 else (ZERO, -difference)
    else:
        gain, loss = (-difference, ZERO) if difference <= 0 else (ZERO, difference)
    return SettlementResult(
        settlement_base_amount=settlement_base,
        carrying_base_amount=carrying,
        realized_gain=gain.quantize(FOUR_PLACES, rounding=ROUND_HALF_UP),
        realized_loss=loss.quantize(FOUR_PLACES, rounding=ROUND_HALF_UP),
        remaining_foreign_amount=(remaining_foreign - settled_foreign).quantize(FOUR_PLACES),
        remaining_carrying_base_amount=(remaining_base - carrying).quantize(FOUR_PLACES),
    )
