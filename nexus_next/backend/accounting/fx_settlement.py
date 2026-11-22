"""Pure FX settlement arithmetic for receivable/payable subledgers.

This module deliberately contains no ORM writes. Callers must persist allocation
rows and journal entries atomically, and must pass the *remaining* foreign and
carrying balances so the final settlement absorbs any prior rounding residual.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

FOUR_PLACES = Decimal("0.0001")
ZERO = Decimal("0.0000")


def _positive(value, field):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a valid decimal amount.") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError(f"{field} must be greater than zero.")
    return amount


def allocate_carrying_base_amount(
    remaining_foreign_amount,
    remaining_carrying_base_amount,
    settlement_foreign_amount,
):
    """Allocate historical base carrying value proportionally to a settlement.

    The last settlement receives the exact remaining carrying amount rather than
    a newly rounded ratio, preventing a residual balance caused by rounding.
    """
    remaining_foreign = _positive(remaining_foreign_amount, "remaining_foreign_amount")
    remaining_base = _positive(remaining_carrying_base_amount, "remaining_carrying_base_amount")
    settled_foreign = _positive(settlement_foreign_amount, "settlement_foreign_amount")
    if settled_foreign > remaining_foreign:
        raise ValueError("Settlement exceeds the remaining foreign-currency balance.")
    if settled_foreign == remaining_foreign:
        return remaining_base.quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)
    return (remaining_base * settled_foreign / remaining_foreign).quantize(
        FOUR_PLACES, rounding=ROUND_HALF_UP
    )


def calculate_realized_fx(direction, carrying_base_amount, settlement_base_amount):
    """Return (realized_gain, realized_loss) for an AR or AP settlement.

    Amounts are in company base currency. For receivables, collecting more than
    carrying value is a gain; for payables, paying less than carrying value is
    a gain. The returned pair is always non-negative and rounded to four places.
    """
    if direction not in {"receivable", "payable"}:
        raise ValueError("direction must be 'receivable' or 'payable'.")
    carrying = _positive(carrying_base_amount, "carrying_base_amount")
    settled = _positive(settlement_base_amount, "settlement_base_amount")
    difference = settled - carrying
    gain = difference if (
        (direction == "receivable" and difference > 0)
        or (direction == "payable" and difference < 0)
    ) else ZERO
    loss = -difference if (
        (direction == "receivable" and difference < 0)
        or (direction == "payable" and difference > 0)
    ) else ZERO
    return (
        gain.quantize(FOUR_PLACES, rounding=ROUND_HALF_UP),
        loss.quantize(FOUR_PLACES, rounding=ROUND_HALF_UP),
    )
