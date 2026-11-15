import sqlite3
import unittest
from datetime import date, timedelta
from decimal import Decimal

from accounting_core import register_decimal_aggregates
from management_reports import build_inventory_ageing, build_period_comparison


class ManagementReportsTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE accounts(
                id INTEGER PRIMARY KEY, code TEXT, name_ar TEXT,
                account_type TEXT, is_group INTEGER DEFAULT 0, active INTEGER DEFAULT 1,
                statement_section TEXT
            );
            CREATE TABLE journal_entries(
                id INTEGER PRIMARY KEY, entry_date TEXT, status TEXT,
                entry_type TEXT DEFAULT 'general'
            );
            CREATE TABLE journal_lines(
                id INTEGER PRIMARY KEY, entry_id INTEGER, account_id INTEGER,
                debit TEXT, credit TEXT
            );
            CREATE TABLE management_account_roles(account_id INTEGER, role TEXT);
            INSERT INTO accounts VALUES
                (1,'4100','مبيعات','revenue',0,1,'revenue'),
                (2,'5100','تكلفة مبيعات','expense',0,1,'direct_costs'),
                (3,'6100','مصروفات','expense',0,1,'operating_expenses');
            INSERT INTO journal_entries VALUES
                (1,'2026-10-05','posted','general'),
                (2,'2026-10-06','posted','general'),
                (3,'2026-10-07','posted','general'),
                (4,'2026-09-05','posted','general'),
                (5,'2026-09-06','posted','general'),
                (6,'2026-09-07','posted','general');
            INSERT INTO journal_lines VALUES
                (1,1,1,'0','1000'),(2,2,2,'300','0'),(3,3,3,'100','0'),
                (4,4,1,'0','800'),(5,5,2,'200','0'),(6,6,3,'100','0');
            CREATE TABLE stock_products(id INTEGER PRIMARY KEY,sku TEXT,name_ar TEXT);
            CREATE TABLE stock_warehouses(id INTEGER PRIMARY KEY,code TEXT,name_ar TEXT);
            CREATE TABLE stock_movements(id INTEGER PRIMARY KEY,movement_date TEXT);
            CREATE TABLE stock_lots(id INTEGER PRIMARY KEY,expiry_date TEXT);
            CREATE TABLE stock_valuation_layers(
                id INTEGER PRIMARY KEY,product_id INTEGER,warehouse_id INTEGER,
                source_movement_id INTEGER,lot_id INTEGER,qty_remaining TEXT,
                unit_cost TEXT,created_at TEXT
            );
            INSERT INTO stock_products VALUES(1,'P-1','صنف تجريبي');
            INSERT INTO stock_warehouses VALUES(1,'W-1','المخزن الرئيسي');
        """)
        register_decimal_aggregates(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_period_comparison_calculates_variance(self):
        report = build_period_comparison(
            self.conn, "2026-10-01", "2026-10-31", "previous_period"
        )
        revenue = next(row for row in report["rows"] if row["key"] == "revenue_total")
        self.assertEqual(revenue["current"], Decimal("1000"))
        self.assertEqual(revenue["previous"], Decimal("800"))
        self.assertEqual(revenue["variance"], Decimal("200"))
        self.assertEqual(revenue["variance_percent"], Decimal("25"))

    def test_inventory_ageing_values_remaining_layers_by_age(self):
        old_date = (date(2026, 11, 15) - timedelta(days=100)).isoformat()
        recent_date = (date(2026, 11, 15) - timedelta(days=10)).isoformat()
        self.conn.executemany(
            """INSERT INTO stock_valuation_layers
               (id,product_id,warehouse_id,source_movement_id,lot_id,
                qty_remaining,unit_cost,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            [
                (1,1,1,None,None,"5","10",old_date+" 09:00:00"),
                (2,1,1,None,None,"2","10",recent_date+" 09:00:00"),
                (3,1,1,None,None,"0","99",old_date+" 09:00:00"),
            ],
        )
        report = build_inventory_ageing(self.conn, "2026-11-15")
        self.assertTrue(report["available"])
        self.assertEqual(report["totals"]["over_90"], Decimal("50"))
        self.assertEqual(report["totals"]["0_30"], Decimal("20"))
        self.assertEqual(report["total_value"], Decimal("70"))
        self.assertEqual(len(report["rows"]), 2)


if __name__ == "__main__":
    unittest.main()
