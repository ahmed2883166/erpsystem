"""PostgreSQL coverage for payroll and employee-grant cash forecasts."""
import os
import unittest
import uuid
from decimal import Decimal


@unittest.skipUnless(
    os.environ.get("NEXUS_TEST_POSTGRES_DSN"),
    "NEXUS_TEST_POSTGRES_DSN is not configured",
)
class ManagementForecastPostgreSQLTests(unittest.TestCase):
    def test_payroll_and_grant_forecasts_subtract_payments_as_of_date(self):
        import psycopg
        from psycopg.rows import dict_row
        from management_reports import _cash_forecast_grants, _cash_forecast_payroll

        schema = "nexus_forecast_" + uuid.uuid4().hex[:12]
        conn = psycopg.connect(
            os.environ["NEXUS_TEST_POSTGRES_DSN"], row_factory=dict_row
        )
        try:
            try:
                with conn.transaction():
                    conn.execute(f'CREATE SCHEMA "{schema}"')
                    conn.execute(f'SET LOCAL search_path TO "{schema}"')
                    conn.execute("""
                        CREATE TABLE payroll_runs(
                            id BIGINT PRIMARY KEY, period TEXT, status TEXT,
                            journal_entry_id BIGINT
                        )
                    """)
                    conn.execute("""
                        CREATE TABLE payroll_lines(
                            id BIGINT PRIMARY KEY, run_id BIGINT, net_pay TEXT
                        )
                    """)
                    conn.execute("""
                        CREATE TABLE payroll_payments(
                            id BIGINT PRIMARY KEY, run_id BIGINT,
                            payment_date TEXT, amount TEXT
                        )
                    """)
                    conn.execute("""
                        CREATE TABLE employee_grants(
                            id BIGINT PRIMARY KEY, grant_name_snapshot TEXT,
                            grant_date TEXT, status TEXT
                        )
                    """)
                    conn.execute("""
                        CREATE TABLE employee_grant_lines(
                            id BIGINT PRIMARY KEY, grant_id BIGINT, amount TEXT
                        )
                    """)
                    conn.execute("""
                        CREATE TABLE employee_grant_payments(
                            id BIGINT PRIMARY KEY, grant_id BIGINT,
                            payment_date TEXT, amount TEXT
                        )
                    """)
                    conn.execute("INSERT INTO payroll_runs VALUES(1,'2026-11','posted',10)")
                    conn.execute("INSERT INTO payroll_lines VALUES(1,1,'1000')")
                    conn.execute("""
                        INSERT INTO payroll_payments VALUES
                        (1,1,'2026-11-10','250'),(2,1,'2026-11-20','100')
                    """)
                    conn.execute("""
                        INSERT INTO employee_grants VALUES
                        (1,'Ramadan grant','2026-11-25','approved'),
                        (2,'Draft grant','2026-11-26','draft')
                    """)
                    conn.execute("""
                        INSERT INTO employee_grant_lines VALUES
                        (1,1,'400'),(2,2,'900')
                    """)
                    conn.execute("INSERT INTO employee_grant_payments VALUES(1,1,'2026-11-12','100')")

                    payroll = _cash_forecast_payroll(conn, "2026-11-15")
                    grants = _cash_forecast_grants(conn, "2026-11-15")

                    self.assertEqual(len(payroll), 1)
                    self.assertEqual(payroll[0]["balance"], Decimal("750"))
                    self.assertEqual(payroll[0]["due_date"], "2026-11-30")
                    self.assertEqual(len(grants), 1)
                    self.assertEqual(grants[0]["balance"], Decimal("300"))
                    self.assertEqual(grants[0]["due_date"], "2026-11-25")
                    raise RuntimeError("rollback disposable schema")
            except RuntimeError as exc:
                self.assertEqual(str(exc), "rollback disposable schema")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
