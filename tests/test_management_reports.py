import sqlite3
import unittest
from datetime import date, timedelta
from decimal import Decimal

from accounting_core import register_decimal_aggregates
from management_reports import build_cash_forecast, build_inventory_ageing, build_period_comparison


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
            CREATE TABLE stock_movements(id INTEGER PRIMARY KEY,product_id INTEGER,source_warehouse_id INTEGER,target_warehouse_id INTEGER,movement_date TEXT);
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

    def test_cash_forecast_includes_unpaid_payroll_and_approved_grants_as_of_date(self):
        self.conn.executescript("""
            CREATE TABLE payroll_runs(
                id INTEGER PRIMARY KEY, period TEXT, status TEXT, journal_entry_id INTEGER
            );
            CREATE TABLE payroll_lines(
                id INTEGER PRIMARY KEY, run_id INTEGER, net_pay TEXT
            );
            CREATE TABLE payroll_payments(
                id INTEGER PRIMARY KEY, run_id INTEGER, payment_date TEXT, amount TEXT
            );
            CREATE TABLE employee_grants(
                id INTEGER PRIMARY KEY, grant_name_snapshot TEXT, grant_date TEXT, status TEXT
            );
            CREATE TABLE employee_grant_lines(
                id INTEGER PRIMARY KEY, grant_id INTEGER, amount TEXT
            );
            CREATE TABLE employee_grant_payments(
                id INTEGER PRIMARY KEY, grant_id INTEGER, payment_date TEXT, amount TEXT
            );
            INSERT INTO payroll_runs VALUES(1,'2026-11','posted',10);
            INSERT INTO payroll_lines VALUES(1,1,'1000');
            INSERT INTO payroll_payments VALUES(1,1,'2026-11-10','300');
            INSERT INTO payroll_payments VALUES(2,1,'2026-11-20','100');
            INSERT INTO employee_grants VALUES(1,'منحة موسمية','2026-11-20','approved');
            INSERT INTO employee_grant_lines VALUES(1,1,'500');
            INSERT INTO employee_grant_payments VALUES(1,1,'2026-11-12','100');
            INSERT INTO employee_grant_payments VALUES(2,1,'2026-11-22','50');
        """)
        report = build_cash_forecast(self.conn, "2026-11-15", 90)
        payroll = next(row for row in report["items"] if row["kind"] == "payroll")
        grant = next(row for row in report["items"] if row["kind"] == "employee_grants")
        self.assertEqual(payroll["due_date"], "2026-11-30")
        self.assertEqual(payroll["balance"], Decimal("700"))
        self.assertEqual(grant["balance"], Decimal("400"))
        self.assertEqual(report["payments"], Decimal("1100"))
        self.assertEqual(report["net_expected"], Decimal("-1100"))
        self.assertTrue(any("الضرائب" in note for note in report["coverage_notes"]))

    def test_period_comparison_calculates_variance(self):
        report = build_period_comparison(
            self.conn, "2026-10-01", "2026-10-31", "previous_period"
        )
        revenue = next(row for row in report["rows"] if row["key"] == "revenue_total")
        self.assertEqual(revenue["current"], Decimal("1000"))
        self.assertEqual(revenue["previous"], Decimal("800"))
        self.assertEqual(revenue["variance"], Decimal("200"))
        self.assertEqual(revenue["variance_percent"], Decimal("25"))

    def test_inventory_ageing_respects_as_of_and_expiry_and_recent_movement(self):
        self.conn.executemany(
            "INSERT INTO stock_lots(id,expiry_date) VALUES(?,?)",
            [(1, "2026-11-01"), (2, "2026-12-01")],
        )
        self.conn.executemany(
            """INSERT INTO stock_valuation_layers
               (id,product_id,warehouse_id,source_movement_id,lot_id,
                qty_remaining,unit_cost,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            [
                (10,1,1,None,1,"3","10","2026-07-01 08:00:00"),
                (11,1,1,None,2,"2","20","2026-11-01 08:00:00"),
                (12,1,1,None,None,"9","99","2026-11-20 08:00:00"),
            ],
        )
        self.conn.executemany(
            """INSERT INTO stock_movements
               (id,product_id,source_warehouse_id,target_warehouse_id,movement_date)
               VALUES(?,?,?,?,?)""",
            [
                (10,1,1,None,"2026-11-10"),
                (11,1,1,None,"2026-12-10"),
            ],
        )
        report = build_inventory_ageing(self.conn, "2026-11-15")
        self.assertEqual(len(report["rows"]), 2)
        self.assertEqual(report["total_value"], Decimal("70"))
        self.assertEqual(report["expiry_totals"]["expired"], Decimal("30"))
        self.assertEqual(report["expiry_totals"]["within_30"], Decimal("40"))
        self.assertTrue(all(row["last_movement_date"] == "2026-11-10" for row in report["rows"]))
        self.assertTrue(all(row["slow_moving"] is False for row in report["rows"]))

    def test_missing_movement_table_does_not_claim_stock_is_slow_moving(self):
        self.conn.execute("DROP TABLE stock_movements")
        self.conn.execute(
            """INSERT INTO stock_valuation_layers
               (id,product_id,warehouse_id,source_movement_id,lot_id,
                qty_remaining,unit_cost,created_at)
               VALUES(20,1,1,NULL,NULL,'4','15','2026-01-01 08:00:00')"""
        )
        report = build_inventory_ageing(self.conn, "2026-11-15")
        self.assertIsNone(report["rows"][0]["slow_moving"])
        self.assertEqual(
            report["rows"][0]["slow_moving_status"],
            "لا توجد بيانات حركات كافية للتصنيف",
        )

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
