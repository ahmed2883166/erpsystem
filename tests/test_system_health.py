import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import system_health
from accounting_core import register_decimal_aggregates


class SystemHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "health-test.sqlite3"
        self.connection = sqlite3.connect(self.db_path)
        self.connection.row_factory = sqlite3.Row
        register_decimal_aggregates(self.connection)
        self.connection.executescript("""
            CREATE TABLE journal_entries (
                id INTEGER PRIMARY KEY,
                status TEXT NOT NULL
            );
            CREATE TABLE journal_lines (
                id INTEGER PRIMARY KEY,
                entry_id INTEGER NOT NULL,
                debit TEXT DEFAULT '0',
                credit TEXT DEFAULT '0'
            );
            CREATE TABLE sales_invoices (
                id INTEGER PRIMARY KEY,
                status TEXT,
                journal_entry_id INTEGER
            );
            CREATE TABLE supplier_bills (
                id INTEGER PRIMARY KEY,
                status TEXT,
                journal_entry_id INTEGER
            );
            CREATE TABLE stock_valuation_layers (
                id INTEGER PRIMARY KEY,
                qty_remaining TEXT
            );
            CREATE TABLE stock_products (id INTEGER PRIMARY KEY);
            CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY,
                created_at TEXT
            );

            INSERT INTO journal_entries(id,status) VALUES
                (1,'posted'), (2,'posted'), (3,'posted'), (4,'draft');
            INSERT INTO journal_lines(id,entry_id,debit,credit) VALUES
                (1,1,'10.00','0'), (2,1,'0','8.00'),
                (3,3,'5.00','0'), (4,3,'0','5.00'),
                (5,4,'20.00','0'), (6,4,'0','20.00');

            INSERT INTO sales_invoices(id,status,journal_entry_id)
                VALUES (1,'posted',NULL);
            INSERT INTO supplier_bills(id,status,journal_entry_id)
                VALUES (1,'posted',4);

            INSERT INTO stock_valuation_layers(id,qty_remaining)
                VALUES (1,'-2.5'), (2,'3.0');
            INSERT INTO stock_products(id) VALUES (1);
        """)
        self.connection.commit()

        self.app = Flask(__name__)
        self.app.secret_key = "test-only-secret"
        self.app.register_blueprint(system_health.health_bp)
        self.app.add_url_rule(
            "/reports/reconciliation", endpoint="reconciliation",
            view_func=lambda: "report"
        )
        self.app.add_url_rule(
            "/stock/reconciliation", endpoint="stock.valuation_reconciliation",
            view_func=lambda: "stock report"
        )
        self.app.add_url_rule(
            "/purchases/integrity", endpoint="purchase.integrity_issues",
            view_func=lambda: "purchase report"
        )
        self.app.add_url_rule(
            "/login", endpoint="login", view_func=lambda: "login"
        )
        self.app.add_url_rule(
            "/", endpoint="dashboard", view_func=lambda: "dashboard"
        )

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def test_detects_unbalanced_empty_documents_and_negative_stock(self):
        with self.app.test_request_context("/system/health"):
            checks = system_health.collect_health_checks(self.connection)

        by_key = {item["key"]: item for item in checks}
        self.assertEqual(by_key["journal_balance"]["count"], 1)
        self.assertEqual(by_key["journal_empty"]["count"], 1)
        self.assertEqual(by_key["missing_journal_sales_invoices"]["count"], 1)
        self.assertEqual(by_key["missing_journal_supplier_bills"]["count"], 1)
        self.assertEqual(by_key["negative_stock_layers"]["count"], 1)

    def test_health_scan_is_read_only(self):
        before = {
            table: self.connection.execute(
                "SELECT COUNT(*) FROM " + table
            ).fetchone()[0]
            for table in (
                "journal_entries", "journal_lines", "sales_invoices",
                "supplier_bills", "stock_valuation_layers"
            )
        }

        with self.app.test_request_context("/system/health"):
            system_health.collect_health_checks(self.connection)

        after = {
            table: self.connection.execute(
                "SELECT COUNT(*) FROM " + table
            ).fetchone()[0]
            for table in before
        }
        self.assertEqual(after, before)

    def test_route_rejects_user_without_health_permission(self):
        with patch.object(system_health, "connect_database", return_value=self.connection):
            client = self.app.test_client()
            with client.session_transaction() as sess:
                sess["user_id"] = 10
                sess["role"] = "staff"
                sess["permissions"] = []

            response = client.get("/system/health")
            self.assertEqual(response.status_code, 302)
            self.assertIn("/", response.headers["Location"])

    def test_route_allows_audit_view_and_hides_restricted_report_links(self):
        with patch.object(system_health, "connect_database", return_value=self.connection):
            client = self.app.test_client()
            with client.session_transaction() as sess:
                sess["user_id"] = 10
                sess["role"] = "staff"
                sess["permissions"] = ["audit.view"]

            response = client.get("/system/health")
            self.assertEqual(response.status_code, 200)
            page = response.get_data(as_text=True)
            self.assertIn("مركز سلامة النظام", page)
            self.assertIn("لا توجد صلاحية لفتح التقرير", page)

    def test_admin_can_open_health_center(self):
        with patch.object(system_health, "connect_database", return_value=self.connection):
            client = self.app.test_client()
            with client.session_transaction() as sess:
                sess["user_id"] = 1
                sess["role"] = "admin"
                sess["permissions"] = []

            response = client.get("/system/health")
            self.assertEqual(response.status_code, 200)
            page = response.get_data(as_text=True)
            self.assertIn("فتح التفاصيل", page)


if __name__ == "__main__":
    unittest.main()
