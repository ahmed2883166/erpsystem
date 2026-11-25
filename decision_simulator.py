"""Non-posting what-if financial scenario calculations for NEXUS ERP.

The simulator deliberately works from posted income-statement totals and never
writes to the company database or creates journal entries.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

CENT = Decimal("0.01")
ZERO = Decimal("0")


def _decimal(value, label, minimum=None, maximum=None):
    try:
        number = Decimal(str(value if value not in (None, "") else "0"))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"قيمة {label} غير صحيحة.")
    if not number.is_finite():
        raise ValueError(f"قيمة {label} غير صحيحة.")
    if minimum is not None and number < minimum:
        raise ValueError(f"قيمة {label} أقل من الحد المسموح.")
    if maximum is not None and number > maximum:
        raise ValueError(f"قيمة {label} أكبر من الحد المسموح.")
    return number


def simulate_decision(statement, sales_volume_change_pct=0,
                      selling_price_change_pct=0, unit_cost_change_pct=0,
                      operating_expense_change_pct=0,
                      additional_operating_expense=0):
    """Return a clearly-labelled scenario based on one posted income statement.

    Percentage inputs are bounded to avoid accidental extreme scenarios.
    Revenue changes with both volume and price; direct costs change with volume
    and unit cost; operating expenses change independently plus a fixed amount.
    """
    volume_pct = _decimal(sales_volume_change_pct, "تغير حجم المبيعات",
                          Decimal("-100"), Decimal("500"))
    price_pct = _decimal(selling_price_change_pct, "تغير سعر البيع",
                         Decimal("-100"), Decimal("500"))
    unit_cost_pct = _decimal(unit_cost_change_pct, "تغير تكلفة الوحدة",
                              Decimal("-100"), Decimal("500"))
    expense_pct = _decimal(operating_expense_change_pct, "تغير المصروفات التشغيلية",
                            Decimal("-100"), Decimal("500"))
    additional = _decimal(additional_operating_expense, "المصروف الإضافي",
                          ZERO, Decimal("999999999999999"))

    revenue = _decimal(statement.get("revenue_total", 0), "الإيرادات")
    cogs = _decimal(statement.get("cogs_total", 0), "تكلفة المبيعات")
    operating = _decimal(statement.get("operating_expense_total", 0), "المصروفات التشغيلية")

    volume_factor = Decimal("1") + volume_pct / Decimal("100")
    price_factor = Decimal("1") + price_pct / Decimal("100")
    cost_factor = Decimal("1") + unit_cost_pct / Decimal("100")
    expense_factor = Decimal("1") + expense_pct / Decimal("100")

    scenario_revenue = (revenue * volume_factor * price_factor).quantize(CENT, rounding=ROUND_HALF_UP)
    scenario_cogs = (cogs * volume_factor * cost_factor).quantize(CENT, rounding=ROUND_HALF_UP)
    scenario_operating = (operating * expense_factor + additional).quantize(CENT, rounding=ROUND_HALF_UP)
    base_gross = revenue - cogs
    scenario_gross = scenario_revenue - scenario_cogs
    base_profit = base_gross - operating
    scenario_profit = scenario_gross - scenario_operating

    def pct(value, base):
        return (value * Decimal("100") / base).quantize(CENT, rounding=ROUND_HALF_UP) if base > ZERO else None

    return {
        "base": {
            "revenue": revenue.quantize(CENT),
            "cogs": cogs.quantize(CENT),
            "gross_profit": base_gross.quantize(CENT),
            "operating_expenses": operating.quantize(CENT),
            "net_profit": base_profit.quantize(CENT),
            "net_margin": pct(base_profit, revenue),
        },
        "scenario": {
            "revenue": scenario_revenue,
            "cogs": scenario_cogs,
            "gross_profit": scenario_gross.quantize(CENT),
            "operating_expenses": scenario_operating,
            "net_profit": scenario_profit.quantize(CENT),
            "net_margin": pct(scenario_profit, scenario_revenue),
        },
        "profit_change": (scenario_profit - base_profit).quantize(CENT),
        "assumptions": {
            "sales_volume_change_pct": volume_pct,
            "selling_price_change_pct": price_pct,
            "unit_cost_change_pct": unit_cost_pct,
            "operating_expense_change_pct": expense_pct,
            "additional_operating_expense": additional.quantize(CENT),
        },
        "notice": "محاكاة تقديرية مبنية على نتائج الفترة المرحلة؛ لا تتنبأ بتغير الطلب تلقائيًا ولا تشمل الضرائب أو التمويل أو أثر المخزون والسيولة.",
    }
