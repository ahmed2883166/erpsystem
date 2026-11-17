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


if __name__ == "__main__":
    unittest.main()
