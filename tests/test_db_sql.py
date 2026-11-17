"""Tests for the opt-in SQL placeholder adapter."""
import os
import sqlite3
import unittest

from db_sql import execute_compat, qmark_to_format


class QmarkTranslationTests(unittest.TestCase):
    def test_translates_only_unquoted_bind_markers(self):
        sql = (
            "SELECT ?, 'literal ? and '' quote', \"identifier?\", "
            "col FROM t WHERE a=? -- comment ?\n"
            "AND b=? /* block ? /* nested ? */ still ? */"
        )
        expected = (
            "SELECT %s, 'literal ? and '' quote', \"identifier?\", "
            "col FROM t WHERE a=%s -- comment ?\n"
            "AND b=%s /* block ? /* nested ? */ still ? */"
        )
        self.assertEqual(qmark_to_format(sql), expected)

    def test_preserves_postgresql_dollar_quoted_bodies(self):
        sql = "SELECT $tag$ ? ' text $tag$, ?"
        self.assertEqual(
            qmark_to_format(sql),
            "SELECT $tag$ ? ' text $tag$, %s",
        )

    def test_rejects_non_string_sql(self):
        with self.assertRaises(TypeError):
            qmark_to_format(None)

    def test_sqlite_execution_keeps_qmark_parameters(self):
        connection = sqlite3.connect(":memory:")
        try:
            row = execute_compat(
                connection,
                "SELECT ? AS first_value, '?' AS literal_value",
                (37,),
            ).fetchone()
            self.assertEqual(row, (37, "?"))
        finally:
            connection.close()


POSTGRES_DSN = os.environ.get("NEXUS_TEST_POSTGRES_DSN", "").strip()


@unittest.skipUnless(POSTGRES_DSN, "NEXUS_TEST_POSTGRES_DSN is not configured")
class PostgreSQLQmarkExecutionTests(unittest.TestCase):
    def test_qmark_parameters_execute_on_postgresql(self):
        from postgres_database import connect_postgres

        connection = connect_postgres(POSTGRES_DSN)
        try:
            row = execute_compat(
                connection,
                "SELECT ?::integer AS value, ?::numeric AS exact_amount",
                (42, "1234567890.123456"),
            ).fetchone()
            self.assertEqual(row["value"], 42)
            self.assertEqual(str(row["exact_amount"]), "1234567890.123456")
        finally:
            connection.rollback()
            connection.close()


if __name__ == "__main__":
    unittest.main()
