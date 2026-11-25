"""PostgreSQL integration tests for financial planning schema initialization."""
import os
import sqlite3
import unittest
import uuid
from decimal import Decimal

from accounting_core import register_decimal_aggregates
from financial_planning import (
    build_budget_comparison, build_budget_monthly_comparison,
    create_financial_year, initialize_planning_schema,
)


POSTGRES_DSN = os.environ.get("NEXUS_TEST_POSTGRES_DSN", "").strip()


@unittest.skipUnless(POSTGRES_DSN, "NEXUS_TEST_POSTGRES_DSN is not configured")
class FinancialPlanningPostgreSQLSchemaTests(unittest.TestCase):
    def test_budget_and_monthly_comparisons_match_sqlite(self):
        import psycopg
        from psycopg.rows import dict_row

        schema = "nexus_budget_parity_" + uuid.uuid4().hex[:12]
        pg = psycopg.connect(POSTGRES_DSN, row_factory=dict_row)
        sqlite_conn = sqlite3.connect(":memory:")
        sqlite_conn.row_factory = sqlite3.Row
        register_decimal_aggregates(sqlite_conn)

        def seed(connection, placeholder, sqlite=False):
            amount_type = "TEXT" if sqlite else "NUMERIC(24,6)"
            connection.execute("""
                CREATE TABLE accounts(id BIGINT PRIMARY KEY,code TEXT,name_ar TEXT,account_type TEXT)
            """)
            connection.execute("""
                CREATE TABLE journal_entries(id BIGINT PRIMARY KEY,entry_date TEXT,status TEXT)
            """)
            connection.execute(f"""
                CREATE TABLE journal_lines(
                    id BIGINT PRIMARY KEY,entry_id BIGINT,account_id BIGINT,
                    debit {amount_type},credit {amount_type}
                )
            """)
            connection.execute("""
                CREATE TABLE budget_plans(
                    id BIGINT PRIMARY KEY,name TEXT,date_from TEXT,date_to TEXT,
                    branch_id BIGINT,cost_center_id BIGINT,project_id BIGINT
                )
            """)
            connection.execute(f"""
                CREATE TABLE budget_lines(
                    id BIGINT PRIMARY KEY,budget_id BIGINT,account_id BIGINT,amount {amount_type}
                )
            """)
            connection.execute(f"""
                CREATE TABLE budget_monthly_lines(
                    id BIGINT PRIMARY KEY,budget_id BIGINT,account_id BIGINT,
                    period TEXT,amount {amount_type}
                )
            """)
            batches = [
                ("INSERT INTO accounts VALUES(" + ",".join([placeholder]*4) + ")",
                 [(1,"4100","Sales","revenue"),(2,"6100","Rent","expense")]),
                ("INSERT INTO journal_entries VALUES(" + ",".join([placeholder]*3) + ")",
                 [(1,"2026-11-05","posted"),(2,"2026-11-06","draft")]),
                ("INSERT INTO journal_lines VALUES(" + ",".join([placeholder]*5) + ")",
                 [(1,1,1,"0","300.25"),(2,1,2,"150.10","0"),(3,2,1,"0","999.00")]),
                ("INSERT INTO budget_plans VALUES(" + ",".join([placeholder]*7) + ")",
                 [(1,"FY 2026 plan","2026-11-01","2026-11-30",None,None,None)]),
                ("INSERT INTO budget_lines VALUES(" + ",".join([placeholder]*4) + ")",
                 [(1,1,1,"250.00"),(2,1,2,"100.00")]),
                ("INSERT INTO budget_monthly_lines VALUES(" + ",".join([placeholder]*5) + ")",
                 [(1,1,1,"2026-11","250.00"),(2,1,2,"2026-11","100.00")]),
            ]
            for sql, values in batches:
                if sqlite:
                    connection.executemany(sql, values)
                else:
                    connection.cursor().executemany(sql, values)

        try:
            seed(sqlite_conn, "?", sqlite=True)
            with pg.transaction():
                pg.execute(f'CREATE SCHEMA "{schema}"')
                pg.execute(f'SET LOCAL search_path TO "{schema}"')
                seed(pg, "%s", sqlite=False)
                _, sqlite_rows, sqlite_totals = build_budget_comparison(sqlite_conn, 1)
                _, pg_rows, pg_totals = build_budget_comparison(pg, 1)

                def normalize(rows):
                    return [
                        (row["id"],row["code"],row["account_type"],row["budget"],
                         row["actual"],row["variance"],row.get("variance_pct"),
                         row["utilization_pct"],row["status"])
                        for row in rows
                    ]
                self.assertEqual(normalize(pg_rows), normalize(sqlite_rows))
                self.assertEqual(pg_totals, sqlite_totals)

                _, pg_months = build_budget_monthly_comparison(pg, 1)
                _, sqlite_months = build_budget_monthly_comparison(sqlite_conn, 1)
                def normalize_months(rows):
                    return [
                        (row["account_id"],row["period"],row["code"],row["account_type"],
                         row["budget"],row["actual"],row["variance"],
                         row["utilization_pct"],row["status"])
                        for row in rows
                    ]
                self.assertEqual(normalize_months(pg_months), normalize_months(sqlite_months))
                self.assertEqual(pg_totals["revenue_actual"], Decimal("300.25"))
                self.assertEqual(pg_totals["expense_actual"], Decimal("150.10"))
                self.assertEqual(next(x for x in pg_months if x["code"]=="4100")["actual"], Decimal("300.25"))
                raise RuntimeError("rollback disposable schema")
        except RuntimeError as exc:
            self.assertEqual(str(exc), "rollback disposable schema")
        finally:
            sqlite_conn.close()
            pg.close()

    def test_schema_initialization_and_immutable_audit_triggers(self):
        from postgres_database import connect_postgres
        import psycopg

        schema = "nexus_financial_planning_" + uuid.uuid4().hex
        admin = connect_postgres(
            POSTGRES_DSN, application_name="NEXUS financial planning schema test", schema="public"
        )
        try:
            admin.execute(f'CREATE SCHEMA "{schema}"')
            admin.commit()
            connection = connect_postgres(
                POSTGRES_DSN, application_name="NEXUS financial planning schema test", schema="public"
            )
            connection.execute(f'SET search_path TO "{schema}"')
            connection.execute("""
                CREATE TABLE accounts (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY
                )
            """)
            connection.execute("""
                CREATE TABLE journal_entries (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    entry_date TEXT NOT NULL,
                    status TEXT NOT NULL
                )
            """)
            connection.commit()

            initialize_planning_schema(connection)
            year_id = create_financial_year(
                connection, "FY 2026 PostgreSQL", "2026-01-01", "2026-12-31", 1
            )
            self.assertGreater(year_id, 0)
            with self.assertRaisesRegex(ValueError, "FY 2026 PostgreSQL"):
                create_financial_year(
                    connection, "Overlapping FY", "2026-06-01", "2027-05-31", 1
                )
            columns = connection.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema=%s AND table_name='journal_entries'
            """, (schema,)).fetchall()
            self.assertIn("entry_type", {row["column_name"] for row in columns})

            plan_id = connection.execute("""
                INSERT INTO budget_plans(name,date_from,date_to)
                VALUES('PostgreSQL test budget','2026-01-01','2026-12-31')
                RETURNING id
            """).fetchone()["id"]
            audit_id = connection.execute("""
                INSERT INTO budget_audit_log(budget_id,action,details)
                VALUES(%s,'created','immutable test')
                RETURNING id
            """, (plan_id,)).fetchone()["id"]
            connection.commit()

            with self.assertRaises(psycopg.Error):
                connection.execute(
                    "UPDATE budget_audit_log SET details='changed' WHERE id=%s",
                    (audit_id,),
                )
            connection.rollback()

            initialize_planning_schema(connection)
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM budget_audit_log WHERE id=%s",
                (audit_id,),
            ).fetchone()["count"]
            self.assertEqual(count, 1)
            connection.rollback()
            connection.close()
        finally:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            admin.commit()
            admin.close()


if __name__ == "__main__":
    unittest.main()
