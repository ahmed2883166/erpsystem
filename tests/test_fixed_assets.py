import sqlite3
import unittest
from decimal import Decimal
from unittest.mock import patch

from fixed_assets import (_asset_disposal_journal_lines, _asset_period_amount,
                          _post_asset_depreciation, _reverse_full_period_depreciation_for_disposal,\n                          _validate_dedicated_accum_account,
                          register_assets_from_journal_lines)


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




class FixedAssetDisposalJournalTests(unittest.TestCase):
    def setUp(self):
        self.asset = {"asset_name": "سيارة نقل", "asset_account_id": 10, "accum_account_id": 11}

    def test_sale_with_gain_and_output_vat_posts_cash_asset_accumulated_and_gain(self):
        lines = _asset_disposal_journal_lines(
            self.asset, Decimal("100000"), Decimal("30000"), Decimal("92000"),
            Decimal("12000"), 20, 30, 40, Decimal("10000")
        )
        self.assertEqual([line[:3] for line in lines], [
            (20, Decimal("92000"), 0), (11, Decimal("30000"), 0),
            (10, 0, Decimal("100000")), (30, 0, Decimal("12000")),
            (40, 0, Decimal("10000")),
        ])
        debit = sum((Decimal(str(line[1])) for line in lines), Decimal("0"))
        credit = sum((Decimal(str(line[2])) for line in lines), Decimal("0"))
        self.assertEqual(debit, credit)

    def test_disposal_without_proceeds_does_not_create_zero_cash_line(self):
        lines = _asset_disposal_journal_lines(
            self.asset, Decimal("100000"), Decimal("30000"), Decimal("0"),
            Decimal("0"), 20, None, 40, Decimal("-70000")
        )
        self.assertEqual([line[0] for line in lines], [11, 10, 40])
        debit = sum((Decimal(str(line[1])) for line in lines), Decimal("0"))
        credit = sum((Decimal(str(line[2])) for line in lines), Decimal("0"))
        self.assertEqual(debit, credit)

    def test_sale_at_loss_is_balanced_and_records_loss_as_debit(self):
        lines = _asset_disposal_journal_lines(
            self.asset, Decimal("100000"), Decimal("30000"), Decimal("60000"),
            Decimal("0"), 20, None, 40, Decimal("-10000")
        )
        self.assertEqual(lines[-1][:3], (40, Decimal("10000"), 0))
        self.assertEqual(sum((Decimal(str(x[1])) for x in lines), Decimal("0")),
                         sum((Decimal(str(x[2])) for x in lines), Decimal("0")))


class FixedAssetDepreciationRevisionTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE asset_depreciations(
                id INTEGER PRIMARY KEY, asset_id INTEGER, period TEXT, amount TEXT,
                journal_entry_id INTEGER UNIQUE, posted_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(asset_id,period)
            );
            CREATE TABLE journal_entries(
                id INTEGER PRIMARY KEY, reference TEXT, status TEXT, entry_date TEXT
            );
            CREATE TABLE journal_lines(
                id INTEGER PRIMARY KEY, entry_id INTEGER, account_id INTEGER,
                debit TEXT, credit TEXT, memo TEXT
            );
            CREATE TABLE asset_depreciation_revisions(
                id INTEGER PRIMARY KEY, depreciation_id INTEGER UNIQUE,
                original_journal_entry_id INTEGER, reversal_journal_entry_id INTEGER UNIQUE,
                replacement_journal_entry_id INTEGER UNIQUE, original_amount TEXT,
                replacement_amount TEXT, disposal_date TEXT, reason TEXT, created_by INTEGER
            );
            INSERT INTO asset_depreciations(id,asset_id,period,amount,journal_entry_id)
                VALUES(1,1,'2026-01','100.00',9),(2,1,'2026-02','100.00',10);
            INSERT INTO journal_entries VALUES(10,'DEP-1-2026-02','posted','2026-02-28');
            INSERT INTO journal_lines VALUES(1,10,30,'100.00','0','مصروف إهلاك');
            INSERT INTO journal_lines VALUES(2,10,40,'0','100.00','مجمع إهلاك');
        """)
        self.asset = {
            "id": 1, "asset_code": "AST-001", "asset_name": "سيارة",
            "ready_date": "2026-01-01", "cost": "1200",
            "residual_value": "0", "useful_life_months": 12,
            "depreciation_method": "straight_line", "annual_rate": "0",
            "expense_account_id": 30, "accum_account_id": 40,
        }

    def tearDown(self):
        self.conn.close()

    def test_full_month_posting_is_reversed_and_replaced_by_daily_amount(self):
        with patch("fixed_assets.post_journal", side_effect=[77, 78]) as post:
            result = _reverse_full_period_depreciation_for_disposal(
                self.conn, self.asset, "2026-02", "2026-02-15", 5
            )
        self.assertEqual(result, (Decimal("53.57"), 77, 78))
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].args[2], "ASSET-DEP-REV-2")
        self.assertEqual(post.call_args_list[0].args[4], [
            (30, "0", "100.00", "عكس إهلاك أصل قبل إعادة احتسابه: مصروف إهلاك"),
            (40, "100.00", "0", "عكس إهلاك أصل قبل إعادة احتسابه: مجمع إهلاك"),
        ])
        row = self.conn.execute(
            "SELECT amount,journal_entry_id FROM asset_depreciations WHERE id=2"
        ).fetchone()
        self.assertEqual((row["amount"], row["journal_entry_id"]), ("53.57", 78))
        revision = self.conn.execute(
            "SELECT original_journal_entry_id,reversal_journal_entry_id,"
            "replacement_journal_entry_id,original_amount,replacement_amount "
            "FROM asset_depreciation_revisions WHERE depreciation_id=2"
        ).fetchone()
        self.assertEqual(tuple(revision), (10, 77, 78, "100.00", "53.57"))

    def test_later_posted_period_blocks_reversal_without_creating_journal(self):
        self.conn.execute(
            "INSERT INTO asset_depreciations(asset_id,period,amount,journal_entry_id) "
            "VALUES(1,'2026-03','100.00',11)"
        )
        with patch("fixed_assets.post_journal") as post:
            with self.assertRaisesRegex(ValueError, "إهلاك لاحق مرحّل"):
                _reverse_full_period_depreciation_for_disposal(
                    self.conn, self.asset, "2026-02", "2026-02-15", 5
                )
        post.assert_not_called()
        self.assertEqual(
            self.conn.execute("SELECT amount FROM asset_depreciations WHERE id=2").fetchone()[0],
            "100.00",
        )


class JournalLinkedAssetRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE journal_entries(id INTEGER PRIMARY KEY, entry_date TEXT, status TEXT);
            CREATE TABLE accounts(id INTEGER PRIMARY KEY, account_type TEXT, is_group INTEGER, active INTEGER);
            CREATE TABLE journal_lines(id INTEGER PRIMARY KEY, entry_id INTEGER, account_id INTEGER,
                debit TEXT, credit TEXT);
            CREATE TABLE fixed_asset_journal_lines(
                journal_line_id INTEGER PRIMARY KEY, entry_id INTEGER, asset_name TEXT,
                category TEXT, ready_date TEXT, cost TEXT, status TEXT, fixed_asset_id INTEGER
            );
            INSERT INTO journal_entries VALUES(40,'2026-11-14','posted');
            INSERT INTO accounts VALUES(10,'asset',0,1);
            INSERT INTO journal_lines VALUES(100,40,10,'25000.00','0');
        """)

    def tearDown(self):
        self.conn.close()

    def test_posted_debit_asset_line_is_staged_at_exact_journal_cost(self):
        result = register_assets_from_journal_lines(
            self.conn, 40,
            [{"asset_name":"سيارة نقل","category":"سيارات","ready_date":"2026-11-20"}],
            7,
        )
        row = self.conn.execute(
            "SELECT * FROM fixed_asset_journal_lines WHERE journal_line_id=100"
        ).fetchone()
        self.assertEqual(result, [100])
        self.assertEqual(row["cost"], "25000.00")
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["entry_id"], 40)

    def test_credit_line_cannot_be_registered_as_fixed_asset(self):
        self.conn.execute(
            "UPDATE journal_lines SET debit='0',credit='25000' WHERE id=100"
        )
        with self.assertRaisesRegex(ValueError, "مدينًا فقط"):
            register_assets_from_journal_lines(
                self.conn, 40,
                [{"asset_name":"سيارة نقل","ready_date":"2026-11-20"}],
                7,
            )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM fixed_asset_journal_lines").fetchone()[0],
            0,
        )

    def test_inactive_or_non_asset_account_is_rejected(self):
        self.conn.execute("UPDATE accounts SET account_type='expense' WHERE id=10")
        with self.assertRaisesRegex(ValueError, "حساب أصل ثابت"):
            register_assets_from_journal_lines(
                self.conn, 40,
                [{"asset_name":"سيارة نقل","ready_date":"2026-11-20"}],
                7,
            )


if __name__ == "__main__":
    unittest.main()
