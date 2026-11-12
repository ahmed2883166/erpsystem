import sqlite3
import unittest
from unittest.mock import patch
from decimal import Decimal

from chart_templates import CHARTS, seed_activity_chart, ensure_activity_chart_accounts
from financial_planning import (initialize_planning_schema, build_balance_sheet_snapshot, _money, _connect, create_financial_year, list_financial_years, create_opening_entry, close_financial_year, reverse_financial_year_entry)
from accounting_core import register_decimal_aggregates, post_journal


class FinancialPlanningTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""CREATE TABLE accounts(
            id INTEGER PRIMARY KEY, code TEXT UNIQUE, name_ar TEXT,
            account_type TEXT, parent_id INTEGER, is_group INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1)""")
        self.conn.executescript("""
            CREATE TABLE journal_entries(id INTEGER PRIMARY KEY,entry_date TEXT,status TEXT);
            CREATE TABLE journal_lines(id INTEGER PRIMARY KEY,entry_id INTEGER,account_id INTEGER,debit TEXT,credit TEXT);
        """)
        register_decimal_aggregates(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_connect_uses_centralized_database_path(self):
        with patch("financial_planning.DB_PATH", "/tmp/nexus-test.db"), \
             patch("financial_planning.connect_database", return_value="connection") as connect:
            self.assertEqual(_connect(), "connection")
            connect.assert_called_once_with("/tmp/nexus-test.db")

    def test_financial_year_creation_and_overlap_protection(self):
        initialize_planning_schema(self.conn)
        year_id = create_financial_year(self.conn, "2026", "2026-01-01", "2026-12-31", 1)
        self.assertGreater(year_id, 0)
        years = list_financial_years(self.conn)
        self.assertEqual(len(years), 1)
        self.assertEqual(years[0]["status"], "open")
        with self.assertRaisesRegex(ValueError, "تتداخل"):
            create_financial_year(self.conn, "FY2026-27", "2026-12-01", "2027-11-30", 1)
        with self.assertRaises(ValueError):
            create_financial_year(self.conn, "Bad dates", "2027-02-01", "2027-01-01", 1)
        self.assertEqual(len(list_financial_years(self.conn)), 1)

    def test_financial_year_overlap_does_not_require_sqlite_row_factory(self):
        conn = sqlite3.connect(":memory:")
        try:
            initialize_planning_schema(conn)
            create_financial_year(conn, "FY 2026", "2026-01-01", "2026-12-31")
            with self.assertRaisesRegex(ValueError, "FY 2026"):
                create_financial_year(conn, "FY 2027", "2026-12-15", "2027-12-14")
        finally:
            conn.close()

    def test_budget_schema_and_decimal_amounts(self):
        initialize_planning_schema(self.conn)
        tables = {r["name"] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("budget_plans", tables)
        self.assertIn("budget_lines", tables)
        self.assertEqual(_money("123.456"), Decimal("123.46"))
        with self.assertRaises(ValueError):
            _money("-1")
        with self.assertRaises(Exception):
            _money("NaN")


    def test_balance_sheet_includes_profit_and_excludes_drafts(self):
        self.conn.executemany("INSERT INTO accounts(id,code,name_ar,account_type,is_group) VALUES(?,?,?,?,0)", [
            (1,"1110","الصندوق","asset"),(2,"3100","رأس المال","equity"),
            (3,"4100","الإيرادات","revenue"),(4,"6100","المصروفات","expense")])
        entries=[
            (1,"2026-01-01","posted",[(1,"1000","0"),(2,"0","1000")]),
            (2,"2026-01-02","posted",[(1,"200","0"),(3,"0","200")]),
            (3,"2026-01-03","posted",[(4,"50","0"),(1,"0","50")]),
            (4,"2026-01-04","draft",[(1,"999","0"),(3,"0","999")]),
        ]
        for entry_id,entry_date,status,lines in entries:
            self.conn.execute("INSERT INTO journal_entries VALUES(?,?,?)",(entry_id,entry_date,status))
            for account_id,debit,credit in lines:
                self.conn.execute("INSERT INTO journal_lines(entry_id,account_id,debit,credit) VALUES(?,?,?,?)",
                                  (entry_id,account_id,debit,credit))
        s=build_balance_sheet_snapshot(self.conn,"2026-01-31")
        self.assertEqual(s["total_assets"],Decimal("1150"))
        self.assertEqual(s["total_liabilities"],Decimal("0"))
        self.assertEqual(s["net_profit"],Decimal("150"))
        self.assertEqual(s["total_equity"],Decimal("1150"))
        self.assertTrue(s["balanced"])

    def test_each_activity_chart_has_unique_account_codes(self):
        for activity, rows in CHARTS.items():
            codes = [row[0] for row in rows]
            self.assertEqual(len(codes), len(set(codes)), activity)

    def test_new_commercial_chart_is_hierarchical(self):
        seed_activity_chart(self.conn, "commercial")
        fixed = self.conn.execute("SELECT id,is_group FROM accounts WHERE code='1240'").fetchone()
        child = self.conn.execute("SELECT parent_id FROM accounts WHERE code='1241'").fetchone()
        wages = self.conn.execute("SELECT id,is_group FROM accounts WHERE code='6600'").fetchone()
        marketing = self.conn.execute("SELECT id,is_group FROM accounts WHERE code='6700'").fetchone()
        self.assertEqual(fixed["is_group"], 1)
        self.assertEqual(child["parent_id"], fixed["id"])
        self.assertEqual(wages["is_group"], 1)
        self.assertEqual(marketing["is_group"], 1)

    def test_legacy_chart_gets_common_branches_without_rewriting_accounts(self):
        self.conn.execute("INSERT INTO accounts(code,name_ar,account_type,is_group) VALUES('1000','أصل قديم','asset',0)")
        original = self.conn.execute("SELECT id,name_ar FROM accounts WHERE code='1000'").fetchone()
        ensure_activity_chart_accounts(self.conn, "commercial")
        after = self.conn.execute("SELECT id,name_ar FROM accounts WHERE code='1000'").fetchone()
        self.assertEqual(tuple(original), tuple(after))
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM accounts WHERE code='1240' AND is_group=1").fetchone())
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM accounts WHERE code='6610'").fetchone())
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM accounts WHERE code='1210' AND is_group=1").fetchone())
        fixed_child = self.conn.execute("SELECT parent_id FROM accounts WHERE code='1211'").fetchone()
        fixed_group = self.conn.execute("SELECT id FROM accounts WHERE code='1210'").fetchone()
        self.assertEqual(fixed_child["parent_id"], fixed_group["id"])




class FinancialYearJournalTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        register_decimal_aggregates(self.conn)
        self.conn.executescript("""
        CREATE TABLE accounts(
            id INTEGER PRIMARY KEY,code TEXT UNIQUE,name_ar TEXT,account_type TEXT,
            parent_id INTEGER,is_group INTEGER DEFAULT 0,active INTEGER DEFAULT 1);
        CREATE TABLE journal_entries(
            id INTEGER PRIMARY KEY,entry_date TEXT NOT NULL,reference TEXT,
            description TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'posted',
            created_by INTEGER,created_at TEXT NOT NULL);
        CREATE TABLE journal_lines(
            id INTEGER PRIMARY KEY,entry_id INTEGER NOT NULL,account_id INTEGER NOT NULL,
            debit TEXT NOT NULL DEFAULT '0',credit TEXT NOT NULL DEFAULT '0',memo TEXT);
        CREATE TABLE accounting_periods(
            id INTEGER PRIMARY KEY,period_name TEXT,start_date TEXT,end_date TEXT,
            locked INTEGER DEFAULT 0,created_at TEXT);
        INSERT INTO accounts VALUES(1,'1110','الصندوق','asset',NULL,0,1);
        INSERT INTO accounts VALUES(2,'2110','الموردون','liability',NULL,0,1);
        INSERT INTO accounts VALUES(3,'3100','رأس المال','equity',NULL,0,1);
        INSERT INTO accounts VALUES(4,'3200','الأرباح المرحلة','equity',NULL,0,1);
        INSERT INTO accounts VALUES(5,'4100','إيرادات المبيعات','revenue',NULL,0,1);
        INSERT INTO accounts VALUES(6,'6100','مصروفات التشغيل','expense',NULL,0,1);
        """)
        initialize_planning_schema(self.conn)
        self.year_id = create_financial_year(
            self.conn, "2026", "2026-01-01", "2026-12-31", 1
        )

    def tearDown(self):
        self.conn.close()

    def _opening(self):
        return create_opening_entry(self.conn, self.year_id,
            {"1": "1000", "2": "200", "3": "800", "4": "0"}, 1)

    def _activity(self):
        post_journal(self.conn, "2026-03-01", "SALE-2026-1", "بيع",
            [(1, "300", "0", "تحصيل نقدي"), (5, "0", "300", "إيراد")], 1)
        self.conn.commit()
        post_journal(self.conn, "2026-04-01", "EXP-2026-1", "مصروف",
            [(6, "100", "0", "مصروف تشغيل"), (1, "0", "100", "سداد")], 1)
        self.conn.commit()

    def _create_closed_previous_year(self):
        previous_id = create_financial_year(
            self.conn, "2025", "2025-01-01", "2025-12-31", 1
        )
        create_opening_entry(
            self.conn, previous_id,
            {"1": "1000", "2": "200", "3": "800", "4": "0"}, 1
        )
        post_journal(self.conn, "2025-03-01", "SALE-2025-1", "بيع سابق",
            [(1, "300", "0", "تحصيل"), (5, "0", "300", "إيراد")], 1)
        self.conn.commit()
        post_journal(self.conn, "2025-04-01", "EXP-2025-1", "مصروف سابق",
            [(6, "100", "0", "مصروف"), (1, "0", "100", "سداد")], 1)
        self.conn.commit()
        close_financial_year(self.conn, previous_id, 1)
        return previous_id

    def test_opening_rejects_when_posted_entries_already_exist_in_year(self):
        post_journal(self.conn, "2026-02-01", "EARLY-POST", "قيد سابق للافتتاح",
            [(1, "50", "0", "مدين"), (2, "0", "50", "دائن")], 1)
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, "قيود مرحلة داخل السنة"):
            self._opening()
        self.assertIsNone(self.conn.execute(
            "SELECT opening_entry_id FROM financial_years WHERE id=?", (self.year_id,)
        ).fetchone()[0])

    def test_opening_must_match_previous_closed_year_balances(self):
        self._create_closed_previous_year()
        with self.assertRaisesRegex(ValueError, "لا تطابق إقفال السنة السابقة"):
            create_opening_entry(self.conn, self.year_id,
                {"1": "1199", "2": "200", "3": "800", "4": "199"}, 1)
        self.assertIsNone(self.conn.execute(
            "SELECT opening_entry_id FROM financial_years WHERE id=?", (self.year_id,)
        ).fetchone()[0])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM journal_entries WHERE reference LIKE 'FY-%-OPEN-%'"
        ).fetchone()[0], 1)

    def test_opening_accepts_balances_matching_previous_closed_year(self):
        self._create_closed_previous_year()
        entry_id = create_opening_entry(self.conn, self.year_id,
            {"1": "1200", "2": "200", "3": "800", "4": "200"}, 1)
        self.assertIsNotNone(entry_id)

    def test_opening_entry_balances_and_links_once(self):
        entry_id = self._opening()
        self.assertEqual(self.conn.execute(
            "SELECT opening_entry_id FROM financial_years WHERE id=?", (self.year_id,)
        ).fetchone()[0], entry_id)
        totals = self.conn.execute(
            "SELECT DECIMAL_SUM(debit),DECIMAL_SUM(credit) FROM journal_lines WHERE entry_id=?",
            (entry_id,)
        ).fetchone()
        self.assertEqual(Decimal(totals[0]), Decimal("1000.00"))
        self.assertEqual(Decimal(totals[1]), Decimal("1000.00"))
        with self.assertRaisesRegex(ValueError, "بالفعل"):
            self._opening()

    def test_unbalanced_opening_is_rejected_without_journal(self):
        with self.assertRaisesRegex(ValueError, "غير متوازن"):
            create_opening_entry(self.conn, self.year_id,
                {"1": "1000", "2": "200", "3": "700"}, 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0], 0)
        self.assertIsNone(self.conn.execute(
            "SELECT opening_entry_id FROM financial_years WHERE id=?", (self.year_id,)
        ).fetchone()[0])

    def test_close_rejects_drafts(self):
        self.conn.execute("""INSERT INTO journal_entries
            (entry_date,reference,description,status,created_by,created_at)
            VALUES('2026-06-01','DRAFT-1','مسودة','draft',1,'now')""")
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, "مسودة"):
            close_financial_year(self.conn, self.year_id, 1)
        year = self.conn.execute("SELECT status,closing_entry_id FROM financial_years WHERE id=?",
                                 (self.year_id,)).fetchone()
        self.assertEqual(year["status"], "open")
        self.assertIsNone(year["closing_entry_id"])

    def test_close_zeroes_revenue_and_expenses_and_trial_balance_balances(self):
        self._opening()
        self._activity()
        closing_id = close_financial_year(self.conn, self.year_id, 1)
        year = self.conn.execute("SELECT status,closing_entry_id FROM financial_years WHERE id=?",
                                 (self.year_id,)).fetchone()
        self.assertEqual(year["status"], "closed")
        self.assertEqual(year["closing_entry_id"], closing_id)
        for account_id in (5, 6):
            row = self.conn.execute("""SELECT DECIMAL_SUM(jl.debit),DECIMAL_SUM(jl.credit)
                FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
                WHERE jl.account_id=? AND je.status='posted'""", (account_id,)).fetchone()
            self.assertEqual(Decimal(row[0] or "0") - Decimal(row[1] or "0"), Decimal("0.00"))
        retained = self.conn.execute("""SELECT DECIMAL_SUM(debit),DECIMAL_SUM(credit)
            FROM journal_lines WHERE account_id=4""").fetchone()
        self.assertEqual(Decimal(retained[1]) - Decimal(retained[0]), Decimal("200.00"))
        totals = self.conn.execute("""SELECT DECIMAL_SUM(jl.debit),DECIMAL_SUM(jl.credit)
            FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
            WHERE je.status='posted'""").fetchone()
        self.assertEqual(Decimal(totals[0]), Decimal(totals[1]))
        snapshot = build_balance_sheet_snapshot(self.conn, "2026-12-31")
        self.assertTrue(snapshot["balanced"])
        with self.assertRaisesRegex(ValueError, "ليست مفتوحة|بالفعل"):
            close_financial_year(self.conn, self.year_id, 1)

    def test_locked_year_end_period_blocks_close(self):
        self._opening()
        self._activity()
        self.conn.execute("""INSERT INTO accounting_periods
            (period_name,start_date,end_date,locked,created_at)
            VALUES('ديسمبر 2026','2026-12-01','2026-12-31',1,'now')""")
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, "مغلقة بالفعل"):
            close_financial_year(self.conn, self.year_id, 1)

    def test_posting_into_closed_year_is_blocked(self):
        self._opening()
        self._activity()
        close_financial_year(self.conn, self.year_id, 1)
        with self.assertRaisesRegex(ValueError, "ليست مفتوحة"):
            post_journal(self.conn, "2026-11-01", "LATE-POST", "قيد بعد الإقفال",
                [(1, "10", "0", "مدين"), (5, "0", "10", "دائن")], 1)
        self.conn.rollback()

    def test_reversing_opening_keeps_original_and_clears_active_link(self):
        opening_id = self._opening()
        reversal_id = reverse_financial_year_entry(
            self.conn, self.year_id, "opening", "2026-01-01", "تصحيح الأرصدة الافتتاحية", 1
        )
        year = self.conn.execute("SELECT status,opening_entry_id FROM financial_years WHERE id=?",
                                 (self.year_id,)).fetchone()
        self.assertEqual(year["status"], "open")
        self.assertIsNone(year["opening_entry_id"])
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM journal_entries WHERE id=? AND status='posted'", (opening_id,)
        ).fetchone())
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM journal_entries WHERE id=? AND status='posted'", (reversal_id,)
        ).fetchone())
        replacement_id = self._opening()
        self.assertNotEqual(opening_id, replacement_id)

    def test_reversing_close_reopens_year_and_keeps_original(self):
        self._opening()
        self._activity()
        closing_id = close_financial_year(self.conn, self.year_id, 1)
        reversal_id = reverse_financial_year_entry(
            self.conn, self.year_id, "closing", "2026-12-31", "تصحيح الإقفال", 1
        )
        year = self.conn.execute("SELECT status,closing_entry_id FROM financial_years WHERE id=?",
                                 (self.year_id,)).fetchone()
        self.assertEqual(year["status"], "open")
        self.assertIsNone(year["closing_entry_id"])
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM journal_entries WHERE id=? AND status='posted'", (closing_id,)
        ).fetchone())
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM journal_entries WHERE id=? AND status='posted'", (reversal_id,)
        ).fetchone())


if __name__ == "__main__":
    unittest.main()
