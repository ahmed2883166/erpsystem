import unittest
from decimal import Decimal

from decision_simulator import simulate_decision


class DecisionSimulatorTests(unittest.TestCase):
    def setUp(self):
        self.statement = {
            "revenue_total": Decimal("10000.00"),
            "cogs_total": Decimal("6000.00"),
            "operating_expense_total": Decimal("2500.00"),
        }

    def test_price_and_volume_changes_are_applied_separately(self):
        result = simulate_decision(
            self.statement, sales_volume_change_pct="10",
            selling_price_change_pct="5", unit_cost_change_pct="2",
            operating_expense_change_pct="4",
        )
        self.assertEqual(result["scenario"]["revenue"], Decimal("11550.00"))
        self.assertEqual(result["scenario"]["cogs"], Decimal("6732.00"))
        self.assertEqual(result["scenario"]["operating_expenses"], Decimal("2600.00"))
        self.assertEqual(result["scenario"]["net_profit"], Decimal("2218.00"))
        self.assertEqual(result["profit_change"], Decimal("-282.00"))

    def test_additional_operating_expense_is_included_without_mutating_source(self):
        original = dict(self.statement)
        result = simulate_decision(self.statement, additional_operating_expense="750.25")
        self.assertEqual(result["scenario"]["net_profit"], Decimal("749.75"))
        self.assertEqual(self.statement, original)

    def test_zero_scenario_reproduces_posted_statement_totals(self):
        result = simulate_decision(self.statement)
        self.assertEqual(result["base"], result["scenario"])
        self.assertEqual(result["profit_change"], Decimal("0.00"))

    def test_invalid_and_out_of_range_inputs_are_rejected(self):
        for value in ("not-a-number", "NaN", "Infinity", "501", "-101"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    simulate_decision(self.statement, sales_volume_change_pct=value)
        with self.assertRaises(ValueError):
            simulate_decision(self.statement, additional_operating_expense="-1")

    def test_zero_revenue_does_not_divide_by_zero_for_margin(self):
        result = simulate_decision({
            "revenue_total": "0", "cogs_total": "0",
            "operating_expense_total": "100",
        })
        self.assertIsNone(result["base"]["net_margin"])
        self.assertIsNone(result["scenario"]["net_margin"])


if __name__ == "__main__":
    unittest.main()
