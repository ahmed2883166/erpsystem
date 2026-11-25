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

    def test_grni_control_account_mismatch_is_visible_without_posting_adjustment(self):
        self.connection.executescript("""
            ALTER TABLE journal_entries ADD COLUMN reference TEXT;
            UPDATE journal_entries SET reference='HEALTH-'||id;
            ALTER TABLE journal_lines ADD COLUMN account_id INTEGER;
            CREATE TABLE accounts(
                id INTEGER PRIMARY KEY,code TEXT,is_group INTEGER,active INTEGER
            );
            INSERT INTO accounts VALUES(2190,'2190',0,1);
            CREATE TABLE company_settings(setting_key TEXT PRIMARY KEY,setting_value TEXT);
            INSERT INTO company_settings VALUES('activity','commercial');
            CREATE TABLE suppliers(id INTEGER PRIMARY KEY,name TEXT);
            INSERT INTO suppliers VALUES(1,'مورد اختبار');
            CREATE TABLE goods_receipts(
                id INTEGER PRIMARY KEY,receipt_no TEXT,receipt_date TEXT,supplier_id INTEGER,
                total_value TEXT,status TEXT,journal_entry_id INTEGER
            );
            INSERT INTO goods_receipts VALUES(1,'GRN-1','2026-11-10',1,'100','posted',1);
            CREATE TABLE supplier_bill_receipts(
                bill_id INTEGER,receipt_id INTEGER,matched_amount TEXT
            );
            INSERT INTO supplier_bill_receipts VALUES(1,1,'40');
            CREATE TABLE stock_movements(
                id INTEGER PRIMARY KEY,accounting_status TEXT,movement_no TEXT
            );
            INSERT INTO stock_movements(id,accounting_status,movement_no)
                VALUES(99,'posted','PUR-GRN-1');
            ALTER TABLE stock_valuation_layers ADD COLUMN unit_cost TEXT;
            ALTER TABLE stock_valuation_layers ADD COLUMN value_remaining TEXT;
            ALTER TABLE stock_valuation_layers ADD COLUMN source_movement_id INTEGER;
            UPDATE stock_valuation_layers SET qty_remaining='4',unit_cost='10',
                value_remaining='40',source_movement_id=99 WHERE id=1;
            UPDATE stock_valuation_layers SET qty_remaining='0',unit_cost='10',
                value_remaining='0' WHERE id=2;
            INSERT INTO journal_lines(id,entry_id,account_id,debit,credit)
                VALUES(20,1,2190,'0','100'),(21,2,2190,'60','0');
        """)
        with self.app.test_request_context("/system/health"):
            checks = system_health.collect_health_checks(self.connection)
        check = next(item for item in checks if item["key"] == "grni_reconciliation")
        self.assertEqual(check["severity"], "high")
        self.assertEqual(check["count"], 1)
        self.assertIn("فرق المطابقة: -20.00", check["detail"])
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0],
            4,
        )

    def test_opening_mismatch_is_exposed_in_health_center_without_mutation(self):
        self.connection.execute("""CREATE TABLE financial_years(
            id INTEGER PRIMARY KEY,year_name TEXT,start_date TEXT,opening_entry_id INTEGER
        )""")
        self.connection.execute(
            "INSERT INTO financial_years VALUES(1,'2027','2027-01-01',77)"
        )
        self.connection.commit()
        report = {"checks": [
            {"code": "opening_integrity", "count": 0,
             "details": "قيد الافتتاح سليم"},
            {"code": "opening_balance_reconciliation", "count": 5,
             "details": "1130: الدفتري 500 مقابل الافتتاح 0",
             "blocking": True},
        ]}
        with patch("financial_planning.build_financial_year_preclose_report",
                   return_value=report):
            with self.app.test_request_context("/system/health"):
                checks = system_health.collect_health_checks(self.connection)
        check = next(item for item in checks
                     if item["key"] == "opening_balance_reconciliation")
        self.assertEqual(check["severity"], "high")
        self.assertEqual(check["count"], 5)
        self.assertIn("1130", check["detail"])
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0],
            4,
        )

    def test_partial_schema_does_not_crash_health_scan(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript("""
            CREATE TABLE sales_invoices (
                id INTEGER PRIMARY KEY, status TEXT, journal_entry_id INTEGER
            );
            INSERT INTO sales_invoices(status,journal_entry_id)
                VALUES ('posted',NULL), ('draft',NULL);
            CREATE TABLE stock_valuation_layers (id INTEGER PRIMARY KEY);
        """)
        try:
            with self.app.test_request_context("/system/health"):
                checks = system_health.collect_health_checks(connection)
            by_key = {item["key"]: item for item in checks}
            self.assertEqual(by_key["missing_journal_sales_invoices"]["count"], 1)
            self.assertEqual(by_key["negative_stock_layers"]["severity"], "info")
            self.assertEqual(by_key["negative_stock_layers"]["count"], 0)
        finally:
            connection.close()

    def test_sqlite_error_during_opening_check_becomes_visible_warning(self):
        self.connection.execute("""CREATE TABLE financial_years(
            id INTEGER PRIMARY KEY,year_name TEXT,start_date TEXT,opening_entry_id INTEGER
        )""")
        self.connection.execute(
            "INSERT INTO financial_years VALUES(1,'2027','2027-01-01',77)"
        )
        self.connection.commit()
        with patch(
            "financial_planning.build_financial_year_preclose_report",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            with self.app.test_request_context("/system/health"):
                checks = system_health.collect_health_checks(self.connection)
        check = next(item for item in checks
                     if item["key"] == "opening_balance_reconciliation")
        self.assertEqual(check["severity"], "high")
        self.assertEqual(check["count"], 1)
        self.assertIn("تعذر التحقق من افتتاح السنة 2027", check["detail"])

    def test_reports_backup_failure_and_recent_login_activity(self):
        self.connection.executescript("""
            CREATE TABLE company_settings(setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL);
            INSERT INTO company_settings VALUES('backup_enabled','1');
            INSERT INTO company_settings VALUES('backup_last_status','failed');
            INSERT INTO company_settings VALUES('backup_last_error','disk unavailable');
            CREATE TABLE login_attempts(
                id INTEGER PRIMARY KEY, username TEXT, remote_addr TEXT, attempted_at TEXT
            );
            INSERT INTO login_attempts(username,remote_addr,attempted_at)
                VALUES('operator','192.0.2.10',datetime('now'));
        """)
        with self.app.test_request_context("/system/health"):
            checks = system_health.collect_health_checks(self.connection)
        by_key = {item["key"]: item for item in checks}
        self.assertEqual(by_key["backup_readiness"]["severity"], "critical")
        self.assertIn("disk unavailable", by_key["backup_readiness"]["detail"])
        self.assertEqual(by_key["recent_login_attempts"]["count"], 1)
        self.assertEqual(by_key["recent_login_attempts"]["severity"], "info")

    def test_counts_persistent_failed_login_audit_events_for_24_hours(self):
        for column, declaration in (
            ("action", "TEXT"),
            ("outcome", "TEXT"),
            ("entity_type", "TEXT"),
        ):
            self.connection.execute(
                "ALTER TABLE audit_log ADD COLUMN " + column + " " + declaration
            )
        self.connection.executemany(
            """INSERT INTO audit_log(created_at,action,outcome,entity_type)
               VALUES(datetime('now'),?,?,?)""",
            [("login_failed", "failure", "auth")] * 20
            + [("login", "success", "auth")],
        )
        self.connection.commit()
        with self.app.test_request_context("/system/health"):
            checks = system_health.collect_health_checks(self.connection)
        login_check = next(item for item in checks if item["key"] == "recent_login_attempts")
        self.assertEqual(login_check["count"], 20)
        self.assertEqual(login_check["severity"], "high")
        self.assertEqual(
            login_check["href"], "/security/activity-monitor?category=login"
        )
        self.assertIn("سجل التدقيق المحفوظ", login_check["detail"])

    def test_reports_pending_approvals_without_approving_them(self):
        self.connection.execute("""
            CREATE TABLE approval_requests(
                id INTEGER PRIMARY KEY, status TEXT NOT NULL
            )
        """)
        self.connection.executemany(
            "INSERT INTO approval_requests(status) VALUES(?)",
            [("pending",), ("approved",), ("requested",)],
        )
        self.connection.commit()
        with self.app.test_request_context("/system/health"):
            checks = system_health.collect_health_checks(self.connection)
        pending = next(item for item in checks if item["key"] == "pending_approvals")
        self.assertEqual(pending["count"], 2)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM approval_requests WHERE status IN ('pending','requested')"
            ).fetchone()[0], 2
        )

    def test_health_scan_is_read_only(self):
        tables = (
            "journal_entries", "journal_lines", "sales_invoices",
            "supplier_bills", "stock_valuation_layers"
        )
        before = {
            table: self.connection.execute(
                "SELECT * FROM " + table + " ORDER BY 1"
            ).fetchall()
            for table in tables
        }

        with self.app.test_request_context("/system/health"):
            system_health.collect_health_checks(self.connection)

        after = {
            table: self.connection.execute(
                "SELECT * FROM " + table + " ORDER BY 1"
            ).fetchall()
            for table in tables
        }
        self.assertEqual(
            {table: [tuple(row) for row in rows] for table, rows in after.items()},
            {table: [tuple(row) for row in rows] for table, rows in before.items()}
        )

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
        with patch.object(system_health, "connect_database", return_value=self.connection), \
             patch.object(system_health, "render_template", return_value="rendered") as render:
            client = self.app.test_client()
            with client.session_transaction() as sess:
                sess["user_id"] = 10
                sess["role"] = "staff"
                sess["permissions"] = ["audit.view"]

            response = client.get("/system/health")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_data(as_text=True), "rendered")
            checks = render.call_args.kwargs["checks"]
            self.assertTrue(all(
                item["href"] is None
                for item in checks
                if item["key"] in {
                    "journal_balance", "journal_empty", "negative_stock_layers",
                    "stock_gl_review", "purchase_integrity"
                }
            ))
            login_check = next(item for item in checks if item["key"] == "recent_login_attempts")
            self.assertEqual(
                login_check["href"], "/security/activity-monitor?category=login"
            )

    def test_admin_can_open_health_center(self):
        with patch.object(system_health, "connect_database", return_value=self.connection), \
             patch.object(system_health, "render_template", return_value="rendered") as render:
            client = self.app.test_client()
            with client.session_transaction() as sess:
                sess["user_id"] = 1
                sess["role"] = "admin"
                sess["permissions"] = []

            response = client.get("/system/health")
            self.assertEqual(response.status_code, 200)
            checks = render.call_args.kwargs["checks"]
            self.assertTrue(all(
                item["href"]
                for item in checks
                if item["key"] in {
                    "journal_balance", "journal_empty", "negative_stock_layers",
                    "stock_gl_review", "purchase_integrity"
                }
            ))


    def test_http_500_logs_original_traceback_without_disclosing_it(self):
        from app import DATA_DIR, handle_internal_server_error
        from flask import Flask

        test_app = Flask("http-500-log-test")
        test_app.register_error_handler(500, handle_internal_server_error)
        test_app.config["PROPAGATE_EXCEPTIONS"] = False

        @test_app.route("/deliberate-test-failure")
        def deliberate_test_failure():
            raise RuntimeError("PrivateTracebackSentinel")

        response = test_app.test_client().get(
            "/deliberate-test-failure", headers={"Accept": "application/json"}
        )
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("PrivateTracebackSentinel", response.get_data(as_text=True))
        self.assertTrue(response.get_json()["request_id"])
        log_path = DATA_DIR / "logs" / "http_500.log"
        self.assertTrue(log_path.exists())
        log_text = log_path.read_text(encoding="utf-8")
        self.assertIn("PrivateTracebackSentinel", log_text)
        self.assertIn("RuntimeError", log_text)


if __name__ == "__main__":
    unittest.main()
