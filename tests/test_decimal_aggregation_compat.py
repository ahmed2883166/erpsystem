"""Exact monetary SUM expression tests for both supported database dialects."""
import os
import sqlite3
import unittest
import uuid
from decimal import Decimal

from accounting_core import decimal_sum_expression, register_decimal_aggregates


class SQLiteDecimalAggregationCompatibilityTests(unittest.TestCase):
    def test_text_money_is_summed_exactly_without_float_coercion(self):
        connection = sqlite3.connect(":memory:")
        try:
            register_decimal_aggregates(connection)
            connection.execute("CREATE TABLE values_to_sum(kind TEXT, amount TEXT)")
            connection.cursor().executemany(
                "INSERT INTO values_to_sum VALUES(?,?)",
                [("debit", "0.10"), ("debit", "0.20"), ("credit", "0.30")],
            )
            expression = decimal_sum_expression(
                connection,
                "CASE WHEN kind='debit' THEN amount ELSE '0' END",
            )
            result = connection.execute(
                f"SELECT {expression} FROM values_to_sum"
            ).fetchone()[0]
            self.assertEqual(Decimal(result), Decimal("0.30"))
        finally:
            connection.close()


@unittest.skipUnless(
    os.environ.get("NEXUS_TEST_POSTGRES_DSN"),
    "NEXUS_TEST_POSTGRES_DSN is not configured",
)
class PostgreSQLDecimalAggregationCompatibilityTests(unittest.TestCase):
    def test_text_money_is_cast_to_numeric_before_exact_sum(self):
        import psycopg

        connection = psycopg.connect(os.environ["NEXUS_TEST_POSTGRES_DSN"])
        schema = "nexus_decimal_sum_" + uuid.uuid4().hex[:12]
        try:
            with connection.transaction():
                connection.execute(f'CREATE SCHEMA "{schema}"')
                connection.execute(f'SET LOCAL search_path TO "{schema}"')
                connection.execute(
                    "CREATE TABLE values_to_sum(kind TEXT, amount TEXT)"
                )
                connection.cursor().executemany(
                    "INSERT INTO values_to_sum VALUES(%s,%s)",
                    [("debit", "0.10"), ("debit", "0.20"), ("credit", "0.30")],
                )
                expression = decimal_sum_expression(
                    connection,
                    "CASE WHEN kind='debit' THEN amount ELSE '0' END",
                )
                result = connection.execute(
                    f"SELECT {expression} FROM values_to_sum"
                ).fetchone()[0]
                self.assertEqual(result, Decimal("0.30"))
                raise RuntimeError("rollback disposable schema")
        except RuntimeError as exc:
            self.assertEqual(str(exc), "rollback disposable schema")
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
