import sqlite3
import unittest
from decimal import Decimal

from chart_templates import CHARTS, seed_activity_chart, ensure_activity_chart_accounts
from financial_planning import initialize_planning_schema, build_balance_sheet_snapshot, _money
from accounting_core import register_decimal_aggregates


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


if __name__ == "__main__":
    unittest.main()
