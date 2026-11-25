"""PostgreSQL smoke test for the shared income statement calculation."""
import os
import unittest
import uuid
from decimal import Decimal


@unittest.skipUnless(
    os.environ.get("NEXUS_TEST_POSTGRES_DSN"),
    "NEXUS_TEST_POSTGRES_DSN is not configured",
)
class IncomeStatementPostgreSQLTests(unittest.TestCase):
    def test_income_statement_uses_numeric_sum_and_driver_compatible_metadata(self):
        import psycopg
        from psycopg.rows import dict_row
        from income_statement import build_income_statement

        schema = "nexus_income_stmt_" + uuid.uuid4().hex[:12]
        connection = psycopg.connect(
            os.environ["NEXUS_TEST_POSTGRES_DSN"], row_factory=dict_row
        )
        try:
            try:
                with connection.transaction():
                    connection.execute(f'CREATE SCHEMA "{schema}"')
                    connection.execute(f'SET LOCAL search_path TO "{schema}"')
                    connection.execute("""
                        CREATE TABLE accounts(
                            id BIGINT PRIMARY KEY, code TEXT, name_ar TEXT,
                            account_type TEXT, is_group INTEGER, active INTEGER,
                            statement_section TEXT, account_subtype TEXT
                        )
                    """)
                    connection.execute("""
                        CREATE TABLE journal_entries(
                            id BIGINT PRIMARY KEY, entry_date TEXT,
                            status TEXT, entry_type TEXT
                        )
                    """)
                    connection.execute("""
                        CREATE TABLE journal_lines(
                            id BIGINT PRIMARY KEY, entry_id BIGINT,
                            account_id BIGINT, debit TEXT, credit TEXT
                        )
                    """)
                    connection.execute("""
                        CREATE TABLE management_account_roles(
                            account_id BIGINT, role TEXT
                        )
                    """)
                    connection.cursor().executemany(
                        "INSERT INTO accounts VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                        [
                            (1, "4100", "الإيرادات", "revenue", 0, 1, "revenue", None),
                            (2, "5100", "تكلفة المبيعات", "expense", 0, 1, "direct_costs", None),
                            (3, "6100", "مصروفات التشغيل", "expense", 0, 1, "operating_expenses", None),
                        ],
                    )
                    connection.execute(
                        "INSERT INTO journal_entries VALUES(1,'2026-11-10','posted','general')"
                    )
                    connection.cursor().executemany(
                        "INSERT INTO journal_lines VALUES(%s,1,%s,%s,%s)",
                        [
                            (1, 1, "0", "0.10"),
                            (2, 1, "0", "0.20"),
                            (3, 2, "0.05", "0"),
                            (4, 3, "0.02", "0"),
                        ],
                    )
                    connection.execute(
                        "INSERT INTO management_account_roles VALUES(2,'direct_cost')"
                    )

                    report = build_income_statement(
                        connection, "2026-11-01", "2026-11-30"
                    )
                    self.assertEqual(report["revenue_total"], Decimal("0.30"))
                    self.assertEqual(report["cogs_total"], Decimal("0.05"))
                    self.assertEqual(report["operating_expense_total"], Decimal("0.02"))
                    self.assertEqual(report["gross_profit"], Decimal("0.25"))
                    self.assertEqual(report["net_profit"], Decimal("0.23"))
                    raise RuntimeError("rollback disposable schema")
            except RuntimeError as exc:
                self.assertEqual(str(exc), "rollback disposable schema")
        finally:
            connection.close()


    def test_sqlite_and_postgresql_income_statements_match_same_fixture(self):
        """Compare the same exact-decimal journal fixture across both engines."""
        import sqlite3
        import psycopg
        from psycopg.rows import dict_row
        from income_statement import build_income_statement

        sqlite_connection = sqlite3.connect(":memory:")
        sqlite_connection.row_factory = sqlite3.Row
        schema = "nexus_income_parity_" + uuid.uuid4().hex[:12]
        pg_connection = psycopg.connect(
            os.environ["NEXUS_TEST_POSTGRES_DSN"], row_factory=dict_row
        )

        def seed(connection, placeholder):
            connection.execute("""
                CREATE TABLE accounts(
                    id BIGINT PRIMARY KEY, code TEXT, name_ar TEXT,
                    account_type TEXT, is_group INTEGER, active INTEGER,
                    statement_section TEXT, account_subtype TEXT
                )
            """)
            connection.execute("""
                CREATE TABLE journal_entries(
                    id BIGINT PRIMARY KEY, entry_date TEXT,
                    status TEXT, entry_type TEXT
                )
            """)
            connection.execute("""
                CREATE TABLE journal_lines(
                    id BIGINT PRIMARY KEY, entry_id BIGINT,
                    account_id BIGINT, debit TEXT, credit TEXT
                )
            """)
            connection.execute("""
                CREATE TABLE management_account_roles(
                    account_id BIGINT, role TEXT
                )
            """)
            account_sql = "INSERT INTO accounts VALUES(" + ",".join([placeholder] * 8) + ")"
            accounts = [
                (1, "4100", "الإيرادات", "revenue", 0, 1, "revenue", None),
                (2, "5100", "تكلفة المبيعات", "expense", 0, 1, "direct_costs", None),
                (3, "6100", "مصروفات التشغيل", "expense", 0, 1, "operating_expenses", None),
            ]
            if placeholder == "?":
                connection.executemany(account_sql, accounts)
                connection.execute(
                    "INSERT INTO journal_entries VALUES(1,'2026-11-10','posted','general')"
                )
                connection.execute(
                    "INSERT INTO journal_entries VALUES(2,'2026-11-11','posted','year_end_closing')"
                )
                connection.executemany(
                    "INSERT INTO journal_lines VALUES(?,?,?,?,?)",
                    [
                        (1, 1, 1, "0", "0.10"),
                        (2, 1, 1, "0", "0.20"),
                        (3, 1, 2, "0.05", "0"),
                        (4, 1, 3, "0.02", "0"),
                        (5, 2, 1, "0", "99.00"),
                    ],
                )
                connection.execute("INSERT INTO management_account_roles VALUES(2,'direct_cost')")
            else:
                connection.cursor().executemany(account_sql, accounts)
                connection.execute(
                    "INSERT INTO journal_entries VALUES(1,'2026-11-10','posted','general')"
                )
                connection.execute(
                    "INSERT INTO journal_entries VALUES(2,'2026-11-11','posted','year_end_closing')"
                )
                connection.cursor().executemany(
                    "INSERT INTO journal_lines VALUES(%s,%s,%s,%s,%s)",
                    [
                        (1, 1, 1, "0", "0.10"),
                        (2, 1, 1, "0", "0.20"),
                        (3, 1, 2, "0.05", "0"),
                        (4, 1, 3, "0.02", "0"),
                        (5, 2, 1, "0", "99.00"),
                    ],
                )
                connection.execute("INSERT INTO management_account_roles VALUES(2,'direct_cost')")

        def normalized(report):
            result = {
                key: report[key]
                for key in (
                    "revenue_total", "cogs_total", "gross_profit",
                    "operating_expense_total", "net_profit",
                )
            }
            for section in ("revenue_rows", "cogs_rows", "operating_expense_rows"):
                result[section] = [
                    (row["code"], row["debit"], row["credit"], row["balance"])
                    for row in report[section]
                ]
            return result

        try:
            seed(sqlite_connection, "?")
            sqlite_report = build_income_statement(
                sqlite_connection, "2026-11-01", "2026-11-30"
            )
            try:
                with pg_connection.transaction():
                    pg_connection.execute(f'CREATE SCHEMA "{schema}"')
                    pg_connection.execute(f'SET LOCAL search_path TO "{schema}"')
                    seed(pg_connection, "%s")
                    pg_report = build_income_statement(
                        pg_connection, "2026-11-01", "2026-11-30"
                    )
                    self.assertEqual(normalized(pg_report), normalized(sqlite_report))
                    self.assertEqual(pg_report["revenue_total"], Decimal("0.30"))
                    self.assertEqual(pg_report["cogs_total"], Decimal("0.05"))
                    self.assertEqual(pg_report["operating_expense_total"], Decimal("0.02"))
                    self.assertEqual(pg_report["net_profit"], Decimal("0.23"))
                    raise RuntimeError("rollback disposable schema")
            except RuntimeError as exc:
                self.assertEqual(str(exc), "rollback disposable schema")
        finally:
            sqlite_connection.close()
            pg_connection.close()


if __name__ == "__main__":
    unittest.main()
