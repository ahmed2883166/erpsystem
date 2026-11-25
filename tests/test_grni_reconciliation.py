import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from decimal import Decimal
from pathlib import Path
from flask import Flask
from jinja2 import ChoiceLoader, DictLoader
import purchases


class GRNIReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE accounts(id INTEGER PRIMARY KEY,code TEXT,name_ar TEXT,active INTEGER DEFAULT 1,is_group INTEGER DEFAULT 0);
            CREATE TABLE suppliers(id INTEGER PRIMARY KEY,name TEXT);
            CREATE TABLE stock_warehouses(id INTEGER PRIMARY KEY,code TEXT,name_ar TEXT);
            CREATE TABLE goods_receipts(id INTEGER PRIMARY KEY,receipt_no TEXT,supplier_id INTEGER,warehouse_id INTEGER,receipt_date TEXT,total_value TEXT,status TEXT);
            CREATE TABLE supplier_bills(id INTEGER PRIMARY KEY,bill_no TEXT,bill_date TEXT,bill_kind TEXT,status TEXT);
            CREATE TABLE supplier_bill_receipts(bill_id INTEGER,receipt_id INTEGER,matched_amount TEXT);
            CREATE TABLE journal_entries(id INTEGER PRIMARY KEY,entry_date TEXT,status TEXT);
            CREATE TABLE journal_lines(id INTEGER PRIMARY KEY,entry_id INTEGER,account_id INTEGER,debit TEXT,credit TEXT);
            INSERT INTO accounts VALUES(1,'2190','بضاعة مستلمة غير مفوترة',1,0);
            INSERT INTO suppliers VALUES(1,'المورد الأول');
            INSERT INTO stock_warehouses VALUES(1,'WH-1','المخزن الرئيسي');
            INSERT INTO goods_receipts VALUES
              (1,'GR-OLD',1,1,'2026-10-20','100.00','posted'),
              (2,'GR-NOV',1,1,'2026-11-10','50.00','posted'),
              (3,'GR-DRAFT',1,1,'2026-11-12','999.00','draft');
            INSERT INTO supplier_bills VALUES
              (1,'BILL-OLD','2026-10-25','inventory','posted'),
              (2,'BILL-NOV','2026-11-20','inventory','posted'),
              (3,'BILL-DRAFT','2026-11-22','inventory','draft');
            INSERT INTO supplier_bill_receipts VALUES
              (1,1,'40.00'),(2,1,'10.00'),(2,2,'20.00'),(3,2,'5.00');
            INSERT INTO journal_entries VALUES
              (1,'2026-10-20','posted'),(2,'2026-10-25','posted'),
              (3,'2026-11-10','posted'),(4,'2026-11-20','posted'),
              (5,'2026-11-25','posted'),(6,'2026-11-26','draft');
            INSERT INTO journal_lines VALUES
              (1,1,1,'0','100.00'),(2,2,1,'40.00','0'),
              (3,3,1,'0','50.00'),(4,4,1,'30.00','0'),
              (5,5,1,'3.00','0'),(6,6,1,'500.00','0');
        """)

    def tearDown(self):
        self.conn.close()

    def test_monthly_subledger_and_gl_reconcile_and_draft_documents_are_excluded(self):
        report = purchases.build_grni_reconciliation(self.conn, "2026-11")
        self.assertEqual(report["subledger_opening"], Decimal("60.00"))
        self.assertEqual(report["period_receipts"], Decimal("50.00"))
        self.assertEqual(report["period_bill_settlements"], Decimal("30.00"))
        self.assertEqual(report["subledger_closing"], Decimal("80.00"))
        self.assertEqual(report["gl_opening"], Decimal("60.00"))
        self.assertEqual(report["gl_period_credit"], Decimal("50.00"))
        self.assertEqual(report["gl_period_debit"], Decimal("33.00"))
        self.assertEqual(report["gl_closing"], Decimal("77.00"))
        self.assertEqual(report["closing_difference"], Decimal("-3.00"))
        self.assertEqual(len(report["rows"]), 2)
        receipt = next(row for row in report["rows"] if row["receipt_no"] == "GR-NOV")
        self.assertEqual(receipt["outstanding"], Decimal("30.00"))

    def test_activity_filter_uses_its_mapped_grni_account_and_subledger(self):
        self.conn.execute("ALTER TABLE goods_receipts ADD COLUMN activity_code TEXT")
        self.conn.execute("ALTER TABLE supplier_bills ADD COLUMN activity_code TEXT")
        self.conn.execute("UPDATE goods_receipts SET activity_code='commercial' WHERE id=1")
        self.conn.execute("UPDATE goods_receipts SET activity_code='service' WHERE id=2")
        self.conn.execute("UPDATE supplier_bills SET activity_code='commercial' WHERE id=1")
        self.conn.execute("UPDATE supplier_bills SET activity_code='service' WHERE id=2")
        self.conn.execute("INSERT INTO accounts VALUES(2,'2290','GRNI خدمات',1,0)")
        self.conn.execute("INSERT INTO journal_entries VALUES(7,'2026-11-10','posted')")
        self.conn.execute("INSERT INTO journal_lines VALUES(7,7,2,'0','50.00')")
        with patch("company_activities.resolve_activity_posting_account", return_value="2290"):
            report = purchases.build_grni_reconciliation(self.conn, "2026-11", "service")
        self.assertEqual(report["activity_code"], "service")
        self.assertEqual(report["account_code"], "2290")
        self.assertEqual(report["period_receipts"], Decimal("50.00"))
        self.assertEqual(report["period_bill_settlements"], Decimal("20.00"))
        self.assertEqual(len(report["rows"]), 1)
        self.assertEqual(report["rows"][0]["receipt_no"], "GR-NOV")
        self.assertEqual(report["gl_period_credit"], Decimal("50.00"))

    def test_invalid_month_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "شهرًا صحيحًا"):
            purchases.build_grni_reconciliation(self.conn, "2026-13")

    def test_missing_grni_account_does_not_claim_a_reconciliation(self):
        self.conn.execute("DELETE FROM accounts WHERE code='2190'")
        report = purchases.build_grni_reconciliation(self.conn, "2026-11")
        self.assertFalse(report["account_available"])
        self.assertIsNone(report["closing_difference"])

    def test_route_is_read_only_and_allows_viewer_role(self):
        original_db_path = purchases.DB_PATH
        try:
            app = Flask(__name__, template_folder="../templates")
            app.secret_key = "test"
            app.jinja_loader = ChoiceLoader([
                DictLoader({"base.html": "{% block content %}{% endblock %}"}),
                app.jinja_loader,
            ])
            app.register_blueprint(purchases.purchase_bp)
            with tempfile.TemporaryDirectory() as tmp:
                db_path = Path(tmp) / "grni.db"
                seed = sqlite3.connect(db_path)
                self.conn.backup(seed)
                seed.close()
                purchases.DB_PATH = db_path
                client = app.test_client()
                with client.session_transaction() as session:
                    session["user_id"] = 7
                    session["role"] = "viewer"
                response = client.get("/purchases/grni-reconciliation?month=2026-11")
                self.assertEqual(response.status_code, 200)
                self.assertIn("تقرير تسوية GRNI الشهري".encode(), response.data)
                check = sqlite3.connect(db_path)
                self.assertEqual(check.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0], 6)
                check.close()
        finally:
            purchases.DB_PATH = original_db_path


if __name__ == "__main__":
    unittest.main()
