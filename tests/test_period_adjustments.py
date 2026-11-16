import sqlite3
import unittest
from decimal import Decimal

from accounting_core import register_decimal_aggregates
from period_adjustments import (
    initialize_adjustments_schema, create_adjustment, preview_adjustment,
    post_adjustment_period, reverse_adjustment_period,
)


class PeriodAdjustmentTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        register_decimal_aggregates(self.conn)
        self.conn.executescript("""
        CREATE TABLE accounts(
          id INTEGER PRIMARY KEY,code TEXT UNIQUE,name_ar TEXT,account_type TEXT,
          is_group INTEGER DEFAULT 0,active INTEGER DEFAULT 1);
        CREATE TABLE journal_entries(
          id INTEGER PRIMARY KEY,entry_date TEXT NOT NULL,reference TEXT,
          description TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'posted',
          created_by INTEGER,created_at TEXT NOT NULL);
        CREATE TABLE journal_lines(
          id INTEGER PRIMARY KEY,entry_id INTEGER NOT NULL,account_id INTEGER NOT NULL,
          debit TEXT NOT NULL DEFAULT '0',credit TEXT NOT NULL DEFAULT '0',memo TEXT);
        CREATE TABLE company_settings(setting_key TEXT PRIMARY KEY,setting_value TEXT NOT NULL);
        INSERT INTO accounts VALUES
          (1,'6100','مصروفات إيجار','expense',0,1),
          (2,'1210','مصروفات مقدمة','asset',0,1),
          (3,'2110','مصروفات مستحقة','liability',0,1),
          (4,'4100','إيرادات خدمات','revenue',0,1),
          (5,'1220','عملاء وإيرادات مستحقة','asset',0,1),
          (6,'2190','مخصصات','liability',0,1);
        """)
        initialize_adjustments_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def _schedule(self, frequency="monthly"):
        return create_adjustment(
            self.conn, "إيجار المكتب", "prepaid_expense", "100.00",
            "2026-01-01", "2026-03-31", frequency, 1, 2, 7
        )

    def test_monthly_allocation_preserves_exact_total(self):
        adjustment_id = self._schedule()
        preview = preview_adjustment(self.conn, adjustment_id)
        self.assertEqual([p["amount"] for p in preview["periods"]],
                         [Decimal("33.33"), Decimal("33.33"), Decimal("33.34")])
        self.assertEqual(sum((p["amount"] for p in preview["periods"]), Decimal("0")),
                         Decimal("100.00"))

    def test_yearly_frequency_creates_12_month_periods_from_start_date(self):
        adjustment_id = create_adjustment(
            self.conn, "تأمين سنوي", "prepaid_expense", "2400",
            "2026-07-01", "2028-06-30", "yearly", 1, 2, 7
        )
        periods = preview_adjustment(self.conn, adjustment_id)["periods"]
        self.assertEqual(len(periods), 2)
        self.assertEqual([p["amount"] for p in periods],
                         [Decimal("1200.00"), Decimal("1200.00")])

    def test_post_is_idempotent_and_creates_balanced_journal(self):
        adjustment_id = self._schedule()
        period = preview_adjustment(self.conn, adjustment_id)["periods"][0]
        entry_id = post_adjustment_period(
            self.conn, adjustment_id, period["period_start"], period["period_end"], 7
        )
        totals = self.conn.execute(
            "SELECT SUM(CAST(debit AS REAL)),SUM(CAST(credit AS REAL)) FROM journal_lines WHERE entry_id=?",
            (entry_id,)
        ).fetchone()
        self.assertAlmostEqual(totals[0], 33.33, places=2)
        self.assertAlmostEqual(totals[1], 33.33, places=2)
        with self.assertRaisesRegex(ValueError, "من قبل"):
            post_adjustment_period(
                self.conn, adjustment_id, period["period_start"], period["period_end"], 7
            )

    def test_reversal_preserves_original_and_cannot_repeat(self):
        adjustment_id = self._schedule()
        period = preview_adjustment(self.conn, adjustment_id)["periods"][0]
        entry_id = post_adjustment_period(
            self.conn, adjustment_id, period["period_start"], period["period_end"], 7
        )
        post_id = self.conn.execute(
            "SELECT id FROM period_adjustment_posts WHERE journal_entry_id=?", (entry_id,)
        ).fetchone()[0]
        reversal_id = reverse_adjustment_period(
            self.conn, post_id, "2026-01-31", "تصحيح تاريخ المستند", 7
        )
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM journal_entries WHERE id=? AND status='posted'", (entry_id,)
        ).fetchone())
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM journal_entries WHERE id=? AND status='posted'", (reversal_id,)
        ).fetchone())
        with self.assertRaisesRegex(ValueError, "بالفعل"):
            reverse_adjustment_period(
                self.conn, post_id, "2026-01-31", "محاولة عكس مكرر", 7
            )

    def test_rejects_wrong_account_nature(self):
        with self.assertRaisesRegex(ValueError, "نوع الحساب"):
            create_adjustment(
                self.conn, "تسوية غير صحيحة", "prepaid_expense", "100",
                "2026-01-01", "2026-01-31", "monthly", 3, 2, 7
            )

    def test_preclose_blocks_unposted_adjustment_periods(self):
        from financial_planning import build_financial_year_preclose_report
        self.conn.executescript("""
        CREATE TABLE financial_years(
          id INTEGER PRIMARY KEY,year_name TEXT,start_date TEXT,end_date TEXT,
          status TEXT DEFAULT 'open',opening_entry_id INTEGER,closing_entry_id INTEGER);
        INSERT INTO financial_years(id,year_name,start_date,end_date,status)
          VALUES(1,'2026','2026-01-01','2026-12-31','open');
        """)
        self._schedule()
        report = build_financial_year_preclose_report(self.conn, 1)
        adjustment_check = next(item for item in report["checks"]
                                if item["code"] == "period_adjustments")
        self.assertTrue(adjustment_check["blocking"])
        self.assertEqual(adjustment_check["count"], 3)

    def test_preclose_checks_asset_depreciation_and_open_fx_transactions(self):
        from financial_planning import build_financial_year_preclose_report
        self.conn.executescript("""
        CREATE TABLE financial_years(
          id INTEGER PRIMARY KEY,year_name TEXT,start_date TEXT,end_date TEXT,
          status TEXT DEFAULT 'open',opening_entry_id INTEGER,closing_entry_id INTEGER);
        INSERT INTO financial_years(id,year_name,start_date,end_date,status)
          VALUES(1,'2026','2026-01-01','2026-12-31','open');
        CREATE TABLE fixed_assets(
          id INTEGER PRIMARY KEY,ready_date TEXT,status TEXT DEFAULT 'active');
        INSERT INTO fixed_assets VALUES(1,'2025-01-01','active');
        CREATE TABLE asset_depreciations(
          id INTEGER PRIMARY KEY,asset_id INTEGER,period TEXT,amount TEXT,journal_entry_id INTEGER);
        INSERT INTO asset_depreciations VALUES(1,1,'2026-01','100',NULL);
        CREATE TABLE fx_transactions(
          id INTEGER PRIMARY KEY,original_foreign TEXT,transaction_date TEXT);
        INSERT INTO fx_transactions VALUES(1,'100','2026-02-01');
        CREATE TABLE fx_settlements(
          id INTEGER PRIMARY KEY,transaction_id INTEGER,settlement_date TEXT,foreign_amount TEXT);
        CREATE TABLE fx_revaluations(
          id INTEGER PRIMARY KEY,transaction_id INTEGER,revaluation_date TEXT);
        """)
        report = build_financial_year_preclose_report(self.conn, 1)
        depreciation = next(item for item in report["checks"]
                            if item["code"] == "asset_depreciation_integrity")
        fx = next(item for item in report["checks"]
                  if item["code"] == "fx_year_end_revaluation")
        self.assertTrue(depreciation["blocking"])
        self.assertEqual(depreciation["count"], 1)
        self.assertEqual(fx["severity"], "warning")
        self.assertEqual(fx["count"], 1)


if __name__ == "__main__":
    unittest.main()
