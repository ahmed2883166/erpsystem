import sqlite3
import unittest
from decimal import Decimal
from unittest.mock import patch

from flask import Flask

import stock


class StockReconciliationReportTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE stock_products(
                id INTEGER PRIMARY KEY, sku TEXT, name_ar TEXT
            );
            CREATE TABLE stock_warehouses(
                id INTEGER PRIMARY KEY, code TEXT, name_ar TEXT, active INTEGER
            );
            CREATE TABLE stock_valuation_layers(
                id INTEGER PRIMARY KEY, product_id INTEGER, warehouse_id INTEGER,
                qty_remaining TEXT, unit_cost TEXT, value_remaining TEXT
            );
            CREATE TABLE accounts(
                id INTEGER PRIMARY KEY, code TEXT, name_ar TEXT,
                active INTEGER, is_group INTEGER
            );
            CREATE TABLE journal_entries(id INTEGER PRIMARY KEY, status TEXT);
            CREATE TABLE journal_lines(
                id INTEGER PRIMARY KEY, entry_id INTEGER, account_id INTEGER,
                debit TEXT, credit TEXT, stock_product_id INTEGER,
                stock_warehouse_id INTEGER
            );
            INSERT INTO stock_products VALUES(1,'P-01','صنف اختبار');
            INSERT INTO stock_warehouses VALUES(1,'WH-1','المخزن الرئيسي',1);
            INSERT INTO stock_valuation_layers
                VALUES(1,1,1,'10','10','100');
            INSERT INTO accounts VALUES(1,'1140','المخزون',1,0);
            INSERT INTO journal_entries VALUES(1,'posted');
            INSERT INTO journal_entries VALUES(2,'posted');
        """)
        self.app = Flask(__name__)
        self.app.secret_key = "test-only"

    def tearDown(self):
        self.conn.close()

    def run_report(self):
        with self.app.test_request_context("/stock/reconciliation"):
            with patch("stock.get_db", return_value=self.conn), \
                 patch("stock.render_template", side_effect=lambda template, **context: context), \
                 patch("activity_profiles.active_posting_accounts", return_value={"inventory": "1140"}), \
                 patch("activity_profiles.resolve_product_account_code", return_value="1140"), \
                 patch("activity_profiles.get_active_activity_profile", return_value={
                     "account_roles": {"inventory": ["1140"]}
                 }):
                return stock.valuation_reconciliation.__wrapped__()

    def test_unallocated_legacy_balance_is_not_falsely_matched_to_product_detail(self):
        self.conn.execute(
            """INSERT INTO journal_lines(
                id,entry_id,account_id,debit,credit,stock_product_id,stock_warehouse_id
            ) VALUES(1,1,1,'80','0',1,1)"""
        )
        self.conn.execute(
            """INSERT INTO journal_lines(
                id,entry_id,account_id,debit,credit
            ) VALUES(2,2,1,'20','0')"""
        )

        report = self.run_report()

        self.assertEqual(report["reconciliation_status"], "matched")
        self.assertEqual(report["total_valuation"], Decimal("100"))
        self.assertEqual(report["total_ledger"], Decimal("100"))
        self.assertEqual(report["total_difference"], Decimal("0"))
        self.assertEqual(report["unallocated_total"], Decimal("20"))
        self.assertEqual(report["detail_difference_count"], 1)
        self.assertEqual(report["detail_rows"][0]["difference"], Decimal("20"))

    def test_real_ledger_shortfall_is_reported_as_account_difference(self):
        self.conn.execute(
            """INSERT INTO journal_lines(
                id,entry_id,account_id,debit,credit,stock_product_id,stock_warehouse_id
            ) VALUES(1,1,1,'80','0',1,1)"""
        )
        self.conn.execute(
            """INSERT INTO journal_lines(
                id,entry_id,account_id,debit,credit
            ) VALUES(2,2,1,'10','0')"""
        )

        report = self.run_report()

        self.assertEqual(report["reconciliation_status"], "review")
        self.assertEqual(report["total_valuation"], Decimal("100"))
        self.assertEqual(report["total_ledger"], Decimal("90"))
        self.assertEqual(report["total_difference"], Decimal("10"))
        self.assertEqual(report["unallocated_total"], Decimal("10"))
        self.assertEqual(report["rows"][0]["status"], "review")


    def test_reconciliation_excludes_copied_opening_entry_from_inventory_gl(self):
        self.conn.execute("ALTER TABLE journal_entries ADD COLUMN entry_date TEXT")
        self.conn.execute("UPDATE journal_entries SET entry_date='2026-10-31' WHERE id=1")
        self.conn.execute("UPDATE journal_entries SET entry_date='2026-11-01' WHERE id=2")
        self.conn.executescript("""
            CREATE TABLE financial_years(id INTEGER PRIMARY KEY,year_name TEXT,start_date TEXT,end_date TEXT,status TEXT,opening_entry_id INTEGER);
            CREATE TABLE financial_year_journal_history(id INTEGER PRIMARY KEY,financial_year_id INTEGER,entry_kind TEXT,action TEXT,entry_id INTEGER,related_entry_id INTEGER);
            INSERT INTO financial_years VALUES(1,'2027','2026-11-01','2027-12-31','open',2);
            INSERT INTO financial_year_journal_history(financial_year_id,entry_kind,action,entry_id)
              VALUES(1,'opening','created',2);
        """)
        self.conn.execute("""INSERT INTO journal_lines(id,entry_id,account_id,debit,credit)
            VALUES(10,1,1,'100','0'),(11,2,1,'100','0')""")
        report=self.run_report()
        self.assertEqual(report["total_valuation"],Decimal("100"))
        self.assertEqual(report["total_ledger"],Decimal("100"))
        self.assertEqual(report["total_difference"],Decimal("0"))


if __name__ == "__main__":
    unittest.main()
