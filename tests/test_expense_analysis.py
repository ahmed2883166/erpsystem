import sqlite3
import unittest
from decimal import Decimal

from accounting_core import register_decimal_aggregates
from expense_analysis import build_expense_analysis


class ExpenseAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        register_decimal_aggregates(self.conn)
        self.conn.executescript("""
            CREATE TABLE accounts(
                id INTEGER PRIMARY KEY, code TEXT, name_ar TEXT,
                account_type TEXT, is_group INTEGER DEFAULT 0, active INTEGER DEFAULT 1
            );
            CREATE TABLE journal_entries(
                id INTEGER PRIMARY KEY, entry_date TEXT, status TEXT,
                entry_type TEXT NOT NULL DEFAULT 'general'
            );
            CREATE TABLE journal_lines(
                id INTEGER PRIMARY KEY, entry_id INTEGER, account_id INTEGER,
                debit TEXT, credit TEXT, branch_id INTEGER,
                cost_center_id INTEGER, project_id INTEGER,
                customer_id INTEGER, salesperson_id INTEGER, supplier_id INTEGER
            );
            CREATE TABLE management_account_roles(account_id INTEGER, role TEXT);
            CREATE TABLE branches(id INTEGER PRIMARY KEY,code TEXT,name_ar TEXT);
            CREATE TABLE cost_centers(id INTEGER PRIMARY KEY,code TEXT,name_ar TEXT);
            CREATE TABLE projects(id INTEGER PRIMARY KEY,code TEXT,name_ar TEXT);
            CREATE TABLE customers(id INTEGER PRIMARY KEY,code TEXT,name TEXT,active INTEGER DEFAULT 1);
            CREATE TABLE salespersons(id INTEGER PRIMARY KEY,code TEXT,name TEXT,active INTEGER DEFAULT 1);
            CREATE TABLE suppliers(id INTEGER PRIMARY KEY,code TEXT,name TEXT,active INTEGER DEFAULT 1);
            INSERT INTO accounts VALUES
                (1,'5100','تكلفة النشاط','expense',0,1),
                (2,'6100','مصروف كهرباء','expense',0,1),
                (3,'4100','إيرادات','revenue',0,1),
                (4,'6110','حساب مصروفات مجموعة','expense',1,1);
            INSERT INTO management_account_roles VALUES(1,'direct_cost');
            INSERT INTO branches VALUES(1,'BR1','فرع القاهرة');
            INSERT INTO cost_centers VALUES(1,'CC1','مركز التشغيل');
            INSERT INTO projects VALUES(1,'P1','مشروع ألف');
            INSERT INTO customers VALUES(1,'C1','عميل ألف',1);
            INSERT INTO salespersons VALUES(1,'SP1','مندوب ألف',1);
            INSERT INTO suppliers VALUES(1,'S1','مورد ألف',1);
            INSERT INTO journal_entries VALUES
                (1,'2026-11-02','posted','general'),
                (2,'2026-11-15','posted','general'),
                (3,'2026-11-20','draft','general'),
                (4,'2026-11-22','posted','year_end_closing'),
                (5,'2026-12-01','posted','general'),
                (6,'2026-11-25','posted','general');
            INSERT INTO journal_lines VALUES
                (1,NULL,NULL,NULL,1,1,'1000','0',1,1,1),
                (2,NULL,NULL,NULL,1,2,'300','0',1,1,1),
                (3,NULL,NULL,NULL,2,2,'200','50',NULL,NULL,NULL),
                (4,NULL,NULL,NULL,3,2,'900','0',1,1,1),
                (5,NULL,NULL,NULL,4,2,'700','0',1,1,1),
                (6,NULL,NULL,NULL,5,2,'400','0',1,1,1),
                (7,6,2,'100000000000000000.01','100000000000000000.00',1,1,1,NULL,NULL,NULL);
        """)
        self.conn.execute("UPDATE journal_lines SET customer_id=1,salesperson_id=1,supplier_id=1 WHERE id=1")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_report_includes_only_posted_nonclosing_expense_accounts_in_period(self):
        report = build_expense_analysis(self.conn, "2026-11-01", "2026-11-30")
        self.assertEqual(report["total"], Decimal("1450.01"))
        self.assertEqual(report["direct_total"], Decimal("1000"))
        self.assertEqual(report["operating_total"], Decimal("450.01"))
        self.assertEqual(len(report["detail_rows"]), 3)
        self.assertEqual(report["monthly_rows"][0]["month"], "2026-11")
        self.assertEqual(report["monthly_rows"][0]["amount"], Decimal("1450"))

    def test_report_filters_dimensions_and_keeps_unassigned_separate(self):
        report = build_expense_analysis(
            self.conn, "2026-11-01", "2026-11-30",
            {"branch_id": 1, "cost_center_id": None, "project_id": None}
        )
        self.assertEqual(report["total"], Decimal("1300.01"))
        self.assertTrue(all(row["branch_id"] == 1 for row in report["detail_rows"]))
        self.assertEqual(report["dimension_rows"][0]["amount"], Decimal("1300"))

    def test_monthly_decimal_net_preserves_precision_and_matches_period_total(self):
        report = build_expense_analysis(self.conn, "2026-11-01", "2026-11-30")
        self.assertEqual(report["monthly_rows"][0]["amount"], Decimal("1450.01"))
        self.assertEqual(sum((row["amount"] for row in report["monthly_rows"]), Decimal("0")),
                         report["total"])

    def test_customer_salesperson_and_supplier_filters_are_supported(self):
        report = build_expense_analysis(
            self.conn, "2026-11-01", "2026-11-30",
            {"customer_id": 1, "salesperson_id": 1, "supplier_id": 1}
        )
        self.assertEqual(report["total"], Decimal("1000"))
        self.assertTrue(all(row["customer_id"] == 1 for row in report["detail_rows"]))
        self.assertTrue(all(row["salesperson_id"] == 1 for row in report["detail_rows"]))
        self.assertTrue(all(row["supplier_id"] == 1 for row in report["detail_rows"]))

    def test_each_dimension_breakdown_reconciles_exactly_to_report_total(self):
        report = build_expense_analysis(self.conn, "2026-11-01", "2026-11-30")
        for dimension in ("الفرع", "مركز التكلفة", "المشروع", "العميل", "المندوب", "المورد"):
            subtotal = sum((row["amount"] for row in report["dimension_rows"]
                            if row["dimension"] == dimension), Decimal("0"))
            self.assertEqual(subtotal, report["total"], dimension)

    def test_zero_activity_period_returns_zero_totals_and_empty_rows(self):
        report = build_expense_analysis(self.conn, "2026-10-01", "2026-10-31")
        self.assertEqual(report["total"], Decimal("0"))
        self.assertEqual(report["detail_rows"], [])
        self.assertEqual(report["monthly_rows"], [])


if __name__ == "__main__":
    unittest.main()
