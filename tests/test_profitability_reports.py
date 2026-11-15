import sqlite3
import unittest
from decimal import Decimal

from accounting_core import register_decimal_aggregates
from profitability_reports import build_profitability_report


class ProfitabilityReportTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE accounts(
                id INTEGER PRIMARY KEY, code TEXT, name_ar TEXT,
                account_type TEXT, is_group INTEGER DEFAULT 0,
                active INTEGER DEFAULT 1, statement_section TEXT
            );
            CREATE TABLE journal_entries(
                id INTEGER PRIMARY KEY, entry_date TEXT, status TEXT,
                entry_type TEXT DEFAULT 'general'
            );
            CREATE TABLE journal_lines(
                id INTEGER PRIMARY KEY, entry_id INTEGER, account_id INTEGER,
                debit TEXT, credit TEXT, branch_id INTEGER, cost_center_id INTEGER
            );
            CREATE TABLE branches(id INTEGER PRIMARY KEY,code TEXT,name_ar TEXT,active INTEGER DEFAULT 1);
            CREATE TABLE cost_centers(id INTEGER PRIMARY KEY,code TEXT,name_ar TEXT,active INTEGER DEFAULT 1);
            CREATE TABLE management_account_roles(account_id INTEGER,role TEXT);
            CREATE TABLE stock_warehouses(id INTEGER PRIMARY KEY,code TEXT,name_ar TEXT);
            CREATE TABLE sales_invoices(
                id INTEGER PRIMARY KEY,invoice_date TEXT,status TEXT,warehouse_id INTEGER,
                subtotal TEXT
            );
            CREATE TABLE sales_invoice_lines(
                id INTEGER PRIMARY KEY,invoice_id INTEGER,cost_total TEXT
            );
            CREATE TABLE sales_credit_notes(
                id INTEGER PRIMARY KEY,invoice_id INTEGER,credit_date TEXT,status TEXT,subtotal TEXT
            );
            CREATE TABLE sales_credit_note_lines(
                id INTEGER PRIMARY KEY,credit_note_id INTEGER,cost_total TEXT
            );
            INSERT INTO accounts VALUES
                (1,'4100','إيرادات','revenue',0,1,'revenue'),
                (2,'5100','تكلفة المبيعات','expense',0,1,'direct_costs'),
                (3,'6100','مصروفات تشغيل','expense',0,1,'operating_expenses');
            INSERT INTO management_account_roles VALUES(2,'direct_cost');
            INSERT INTO branches VALUES(1,'B1','فرع القاهرة',1);
            INSERT INTO cost_centers VALUES(1,'CC1','نشاط التجارة',1);
            INSERT INTO stock_warehouses VALUES(1,'W1','مخزن رئيسي');
            INSERT INTO journal_entries VALUES
                (1,'2026-11-01','posted','general'),
                (2,'2026-11-02','posted','general'),
                (3,'2026-11-03','posted','general'),
                (4,'2026-11-04','draft','general'),
                (5,'2026-11-05','posted','year_end_closing');
            INSERT INTO journal_lines VALUES
                (1,1,1,'0','1000',1,1),
                (2,2,2,'200','0',1,1),
                (3,3,3,'100','0',1,1),
                (4,4,1,'0','900',1,1),
                (5,5,1,'0','800',1,1);
            INSERT INTO sales_invoices VALUES(1,'2026-11-01','posted',1,'1000');
            INSERT INTO sales_invoice_lines VALUES(1,1,'200');
            INSERT INTO sales_credit_notes VALUES(1,1,'2026-11-02','posted','100');
            INSERT INTO sales_credit_note_lines VALUES(1,1,'20');
        """)
        register_decimal_aggregates(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_branch_and_cost_center_profit_uses_posted_gl_and_excludes_closing(self):
        report = build_profitability_report(self.conn, "2026-11-01", "2026-11-30")
        branch = report["branches"][0]
        center = report["cost_centers"][0]
        for row in (branch, center):
            self.assertEqual(row["revenue"], Decimal("1000"))
            self.assertEqual(row["cogs"], Decimal("200"))
            self.assertEqual(row["gross_profit"], Decimal("800"))
            self.assertEqual(row["operating_expenses"], Decimal("100"))
            self.assertEqual(row["net_profit"], Decimal("700"))
        self.assertEqual(branch["journal_count"], 3)

    def test_warehouse_profit_subtracts_posted_credit_notes_and_costs(self):
        report = build_profitability_report(self.conn, "2026-11-01", "2026-11-30")
        warehouse = report["warehouses"][0]
        self.assertEqual(warehouse["revenue"], Decimal("900"))
        self.assertEqual(warehouse["cogs"], Decimal("180"))
        self.assertEqual(warehouse["gross_profit"], Decimal("720"))
        self.assertIsNone(warehouse["net_profit"])

    def test_warehouse_report_respects_period_and_posted_status(self):
        self.conn.executescript("""
            INSERT INTO sales_invoices VALUES
                (2,'2026-10-31','posted',1,'300'),
                (3,'2026-11-20','draft',1,'400'),
                (4,'2026-12-01','posted',1,'500');
            INSERT INTO sales_invoice_lines VALUES
                (2,2,'60'),(3,3,'80'),(4,4,'100');
            INSERT INTO sales_credit_notes VALUES
                (2,1,'2026-10-31','posted','25'),
                (3,1,'2026-11-30','draft','35'),
                (4,1,'2026-12-01','posted','45');
            INSERT INTO sales_credit_note_lines VALUES
                (2,2,'5'),(3,3,'7'),(4,4,'9');
        """)
        report = build_profitability_report(self.conn, "2026-11-01", "2026-11-30")
        warehouse = report["warehouses"][0]
        self.assertEqual(warehouse["revenue"], Decimal("900"))
        self.assertEqual(warehouse["cogs"], Decimal("180"))

    def test_warehouse_without_assignment_is_reported_separately(self):
        self.conn.execute(
            "INSERT INTO sales_invoices VALUES(2,'2026-11-10','posted',NULL,'250')"
        )
        self.conn.execute(
            "INSERT INTO sales_invoice_lines VALUES(2,2,'50')"
        )
        report = build_profitability_report(self.conn, "2026-11-01", "2026-11-30")
        unassigned = next(row for row in report["warehouses"] if row["unassigned"])
        self.assertEqual(unassigned["name"], "فواتير دون مخزن محدد")
        self.assertEqual(unassigned["revenue"], Decimal("250"))
        self.assertEqual(unassigned["cogs"], Decimal("50"))
        self.assertIsNone(unassigned["net_profit"])

    def test_asset_disposal_loss_is_not_misreported_as_negative_revenue(self):
        self.conn.executescript("""
            CREATE TABLE asset_disposals(
                id INTEGER PRIMARY KEY, disposal_date TEXT,
                gain_loss_account_id INTEGER, gain_loss_amount TEXT,
                journal_entry_id INTEGER
            );
            INSERT INTO journal_entries VALUES
                (6,'2026-11-06','posted','general'),
                (7,'2026-11-07','posted','general');
            INSERT INTO journal_lines VALUES
                (6,6,1,'0','30',1,1),
                (7,7,1,'40','0',1,1);
            INSERT INTO asset_disposals VALUES
                (1,'2026-11-06',1,'30',6),
                (2,'2026-11-07',1,'-40',7);
        """)
        report = build_profitability_report(self.conn, "2026-11-01", "2026-11-30")
        branch = report["branches"][0]
        self.assertEqual(branch["revenue"], Decimal("1030"))
        self.assertEqual(branch["operating_expenses"], Decimal("140"))
        self.assertEqual(branch["net_profit"], Decimal("690"))

    def test_unassigned_branch_and_cost_center_movements_are_not_hidden(self):
        self.conn.execute(
            "INSERT INTO journal_entries VALUES(6,'2026-11-06','posted','general')"
        )
        self.conn.execute(
            "INSERT INTO journal_lines VALUES(6,6,1,'0','50',NULL,NULL)"
        )
        report = build_profitability_report(self.conn, "2026-11-01", "2026-11-30")
        branch_unassigned = next(row for row in report["branches"] if row["unassigned"])
        center_unassigned = next(row for row in report["cost_centers"] if row["unassigned"])
        self.assertEqual(branch_unassigned["revenue"], Decimal("50"))
        self.assertEqual(center_unassigned["revenue"], Decimal("50"))


if __name__ == "__main__":
    unittest.main()
