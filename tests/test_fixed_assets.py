import sqlite3
import unittest
from decimal import Decimal
from unittest.mock import patch

from fixed_assets import (_asset_period_amount, _post_asset_depreciation,
                          _validate_dedicated_accum_account)


class FixedAssetDepreciationCalculationTests(unittest.TestCase):
    def setUp(self):
        self.asset = {
            "id": 1,
            "asset_code": "M-001",
            "asset_name": "Machine",
            "ready_date": "2026-01-16",
            "cost": "120000",
            "residual_value": "12000",
            "useful_life_months": 60,
            "depreciation_method": "straight_line",
            "annual_rate": "0",
        }

    def test_straight_line_full_month(self):
        amount = _asset_period_amount(self.asset, Decimal("0"), "2026-02")
        self.assertEqual(amount, Decimal("1800.00"))

    def test_straight_line_first_month_is_prorated_from_ready_date(self):
        amount = _asset_period_amount(self.asset, Decimal("0"), "2026-01")
        self.assertEqual(amount, Decimal("929.03"))

    def test_disposal_month_is_prorated_through_disposal_date(self):
        amount = _asset_period_amount(
            self.asset, Decimal("0"), "2026-01", cutoff_date="2026-01-20"
        )
        self.assertEqual(amount, Decimal("290.32"))

    def test_final_partial_month_clears_only_remaining_depreciable_amount(self):
        accumulated = Decimal("107129.03")
        amount = _asset_period_amount(self.asset, accumulated, "2031-01")
        self.assertEqual(amount, Decimal("870.97"))
        self.assertEqual(accumulated + amount, Decimal("108000.00"))

    def test_depreciation_never_exceeds_remaining_depreciable_base(self):
        amount = _asset_period_amount(self.asset, Decimal("107999.99"), "2026-02")
        self.assertEqual(amount, Decimal("0.01"))

    def test_declining_balance_uses_annual_rate_and_monthly_period(self):
        declining = dict(self.asset, depreciation_method="declining_balance", annual_rate="20")
        amount = _asset_period_amount(declining, Decimal("0"), "2026-02")
        self.assertEqual(amount, Decimal("2000.00"))


    def test_posting_creates_one_record_and_dates_journal_at_month_end(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE asset_depreciations(
            id INTEGER PRIMARY KEY, asset_id INTEGER, period TEXT, amount TEXT,
            journal_entry_id INTEGER UNIQUE, UNIQUE(asset_id,period)
        )""")
        asset = dict(self.asset, id=7, ready_date="2026-01-01",
                     cost="1200", residual_value="0", useful_life_months=12,
                     asset_account_id=10, accum_account_id=11, expense_account_id=12)
        with patch("fixed_assets.post_journal", return_value=55) as post:
            amount, entry_id = _post_asset_depreciation(conn, asset, "2026-01", 9)
        self.assertEqual(amount, Decimal("100.00"))
        self.assertEqual(entry_id, 55)
        self.assertEqual(post.call_args.args[1], "2026-01-31")
        journal_lines = post.call_args.args[4]
        self.assertEqual(journal_lines[0][0], 12)
        self.assertEqual(journal_lines[0][1:3], (Decimal("100.00"), 0))
        self.assertEqual(journal_lines[1][0], 11)
        self.assertEqual(journal_lines[1][1:3], (0, Decimal("100.00")))
        row = conn.execute(
            "SELECT period,amount,journal_entry_id FROM asset_depreciations"
        ).fetchone()
        self.assertEqual((row["period"], row["amount"], row["journal_entry_id"]),
                         ("2026-01", "100.00", 55))
        conn.close()

    def test_posting_rejects_a_gap_in_the_depreciation_sequence(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE asset_depreciations(
            id INTEGER PRIMARY KEY, asset_id INTEGER, period TEXT, amount TEXT,
            journal_entry_id INTEGER UNIQUE, UNIQUE(asset_id,period)
        )""")
        asset = dict(self.asset, id=8, ready_date="2026-01-01",
                     cost="1200", residual_value="0", useful_life_months=12,
                     asset_account_id=10, accum_account_id=11, expense_account_id=12)
        with patch("fixed_assets.post_journal") as post:
            with self.assertRaisesRegex(ValueError, "الفترة السابقة"):
                _post_asset_depreciation(conn, asset, "2026-02", 9)
        post.assert_not_called()
        conn.close()

    def test_dedicated_accumulated_depreciation_account_rejects_reuse(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE fixed_assets(id INTEGER PRIMARY KEY, accum_account_id INTEGER);
            CREATE TABLE journal_lines(id INTEGER PRIMARY KEY, account_id INTEGER);
        """)
        _validate_dedicated_accum_account(conn, 22)
        conn.execute("INSERT INTO fixed_assets(accum_account_id) VALUES(22)")
        with self.assertRaisesRegex(ValueError, "مستخدم لأصل آخر"):
            _validate_dedicated_accum_account(conn, 22)
        conn.execute("DELETE FROM fixed_assets")
        conn.execute("INSERT INTO journal_lines(account_id) VALUES(22)")
        with self.assertRaisesRegex(ValueError, "حركات سابقة"):
            _validate_dedicated_accum_account(conn, 22)
        conn.close()

    def test_period_before_ready_date_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "قبل جاهزية"):
            _asset_period_amount(self.asset, Decimal("0"), "2025-12")

    def test_period_after_useful_life_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "انتهى العمر"):
            _asset_period_amount(self.asset, Decimal("0"), "2031-02")


if __name__ == "__main__":
    unittest.main()
