import sqlite3
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as nexus_app
from data_reset import PRESERVED_TABLES, clear_company_data


class CompanyDataResetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "reset.db"
        connection = sqlite3.connect(self.db_path)
        connection.executescript("""
            PRAGMA foreign_keys=ON;
            CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT, role TEXT, active INTEGER, password_hash TEXT);
            CREATE TABLE accounts(id INTEGER PRIMARY KEY, code TEXT);
            CREATE TABLE company_settings(setting_key TEXT PRIMARY KEY, setting_value TEXT);
            CREATE TABLE currencies(code TEXT PRIMARY KEY, name TEXT);
            CREATE TABLE tax_rates(id INTEGER PRIMARY KEY, code TEXT);
            CREATE TABLE audit_log(id INTEGER PRIMARY KEY, action TEXT);
            CREATE TABLE login_attempts(id INTEGER PRIMARY KEY, username TEXT, remote_addr TEXT, attempted_at TEXT);
            CREATE TABLE activity_profiles(id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE cash_flow_account_mappings(id INTEGER PRIMARY KEY, account_id INTEGER);
            CREATE TABLE management_account_roles(id INTEGER PRIMARY KEY, account_id INTEGER);
            CREATE TABLE payroll_rule_sets(id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE payroll_tax_brackets(id INTEGER PRIMARY KEY, rule_set_id INTEGER);
            CREATE TABLE payroll_tax_tier_brackets(id INTEGER PRIMARY KEY, rule_set_id INTEGER);
            CREATE TABLE fx_exchange_rates(id INTEGER PRIMARY KEY, currency_code TEXT);
            CREATE TABLE financial_years(id INTEGER PRIMARY KEY, year_name TEXT);
            CREATE TABLE accounting_periods(id INTEGER PRIMARY KEY, period_name TEXT);
            CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE sales_invoices(
                id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id), total TEXT
            );
            CREATE TABLE sales_invoice_lines(
                id INTEGER PRIMARY KEY, invoice_id INTEGER REFERENCES sales_invoices(id), amount TEXT
            );
            CREATE TRIGGER audit_log_block_delete BEFORE DELETE ON audit_log
            BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
            INSERT INTO users VALUES(1, 'admin', 'admin', 1, 'root-hash');
            INSERT INTO accounts VALUES(1, '1000');
            INSERT INTO company_settings VALUES('company_name', 'NEXUS Test');
            INSERT INTO currencies VALUES('EGP', 'Egyptian Pound');
            INSERT INTO tax_rates VALUES(1, 'VAT14');
            INSERT INTO audit_log VALUES(1, 'before_reset');
            INSERT INTO activity_profiles VALUES(1, 'commercial');
            INSERT INTO cash_flow_account_mappings VALUES(1, 1);
            INSERT INTO management_account_roles VALUES(1, 1);
            INSERT INTO payroll_rule_sets VALUES(1, 'default');
            INSERT INTO payroll_tax_brackets VALUES(1, 1);
            INSERT INTO payroll_tax_tier_brackets VALUES(1, 1);
            INSERT INTO fx_exchange_rates VALUES(1, 'USD');
            INSERT INTO financial_years VALUES(1, '2026');
            INSERT INTO accounting_periods VALUES(1, '2026-01');
            INSERT INTO customers VALUES(1, 'Customer');
            INSERT INTO sales_invoices VALUES(1, 1, '100');
            INSERT INTO sales_invoice_lines VALUES(1, 1, '100');
        """)
        connection.commit()
        connection.close()

    def tearDown(self):
        self.temp.cleanup()

    def open_database(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def test_reset_clears_business_data_and_preserves_system_configuration(self):
        connection = self.open_database()
        result = clear_company_data(connection)
        self.assertGreater(result["deleted_tables"], 0)
        self.assertEqual(result["deleted_rows"], 5)
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM sales_invoices").fetchone()[0], 0
        )
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM customers").fetchone()[0], 0
        )
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM users WHERE username='admin'").fetchone()[0], 1
        )
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 1)
        self.assertEqual(
            connection.execute("SELECT setting_value FROM company_settings WHERE setting_key='company_name'").fetchone()[0],
            "NEXUS Test",
        )
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0], 1)
        self.assertTrue(PRESERVED_TABLES.issubset(set(result["preserved_tables"])))
        self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        connection.close()

    def _seed_route_database(self):
        connection = sqlite3.connect(self.db_path)
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY, username TEXT, role TEXT, active INTEGER,
                password_hash TEXT
            );
            CREATE TABLE IF NOT EXISTS company_settings(
                setting_key TEXT PRIMARY KEY, setting_value TEXT
            );
            CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY, action TEXT);
            CREATE TABLE IF NOT EXISTS login_attempts(id INTEGER PRIMARY KEY, username TEXT, remote_addr TEXT, attempted_at TEXT);
            CREATE TABLE IF NOT EXISTS sample_business_data(id INTEGER PRIMARY KEY, value TEXT);
        """)
        connection.execute(
            "INSERT OR REPLACE INTO users VALUES(1,'admin','admin',1,?)",
            (nexus_app.password_hash("RootSecret123!"),),
        )
        connection.execute("INSERT OR REPLACE INTO sample_business_data VALUES(1,'must-clear')")
        connection.commit()
        connection.close()

    def test_reset_route_requires_root_admin_password_and_creates_backup_first(self):
        self._seed_route_database()
        client = nexus_app.app.test_client()
        with client.session_transaction() as session:
            session.update(
                user_id=1, username="admin", role="admin",
                permissions=["backup.manage"],
            )
        backup_path = Path(self.temp.name) / "pre-reset.zip"

        def open_database():
            return self.open_database()

        with patch.dict(nexus_app.app.config, {"WTF_CSRF_ENABLED": False}):
            with patch.object(nexus_app, "db", side_effect=open_database), \
                 patch.object(nexus_app, "create_backup_archive", return_value=backup_path) as backup, \
                 patch.object(nexus_app, "log") as audit:
                response = client.post(
                    "/backup/reset-data",
                    data={"super_admin_password": "RootSecret123!"},
                )
        self.assertEqual(response.status_code, 302)
        backup.assert_called_once()
        audit.assert_called_once()
        connection = self.open_database()
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM sample_business_data").fetchone()[0], 0)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM users WHERE username='admin'").fetchone()[0], 1)
        connection.close()

    def test_reset_route_rejects_wrong_root_password_without_backup_or_deletion(self):
        self._seed_route_database()
        client = nexus_app.app.test_client()
        with client.session_transaction() as session:
            session.update(
                user_id=1, username="admin", role="admin",
                permissions=["backup.manage"],
            )

        def open_database():
            return self.open_database()

        with patch.dict(nexus_app.app.config, {"WTF_CSRF_ENABLED": False}):
            with patch.object(nexus_app, "db", side_effect=open_database), \
                 patch.object(nexus_app, "create_backup_archive") as backup, \
                 patch.object(nexus_app, "log"):
                response = client.post(
                    "/backup/reset-data",
                    data={"super_admin_password": "wrong-password"},
                )
        self.assertEqual(response.status_code, 302)
        backup.assert_not_called()
        connection = self.open_database()
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM sample_business_data").fetchone()[0], 1)
        connection.close()


    def test_reset_form_includes_csrf_token_and_accepts_protected_post(self):
        self._seed_route_database()
        client = nexus_app.app.test_client()
        with client.session_transaction() as session:
            session.update(
                user_id=1, username="admin", role="admin",
                permissions=["backup.manage"],
            )

        def open_database():
            return self.open_database()

        backup_path = Path(self.temp.name) / "csrf-reset-backup.zip"
        with patch.object(nexus_app, "db", side_effect=open_database) as db_patch:
            with patch.object(
                nexus_app, "create_backup_archive", return_value=backup_path
            ) as backup:
                with patch.object(nexus_app, "log"):
                    page = client.get("/backup/settings")
                    self.assertEqual(page.status_code, 200)
                    html = page.get_data(as_text=True)
                    token_match = re.search(
                        r'name="csrf_token" value="([^"]+)"', html
                    )
                    self.assertIsNotNone(
                        token_match, "backup settings forms must include a CSRF token"
                    )
                    response = client.post(
                        "/backup/reset-data",
                        data={
                            "csrf_token": token_match.group(1),
                            "super_admin_password": "RootSecret123!",
                        },
                    )

        self.assertEqual(response.status_code, 302)
        backup.assert_called_once()
        connection = self.open_database()
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM sample_business_data").fetchone()[0],
            0,
        )
        connection.close()


if __name__ == "__main__":
    unittest.main()
