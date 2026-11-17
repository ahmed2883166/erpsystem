"""Integration tests for the isolated PostgreSQL connection prototype.

The suite skips cleanly when a disposable PostgreSQL DSN is not configured.
It never reads or writes the NEXUS company database.
"""
import os
import unittest
from decimal import Decimal

from postgres_database import connect_postgres


POSTGRES_DSN = os.environ.get("NEXUS_TEST_POSTGRES_DSN", "").strip()


@unittest.skipUnless(POSTGRES_DSN, "NEXUS_TEST_POSTGRES_DSN is not configured")
class PostgreSQLConnectionPrototypeTests(unittest.TestCase):
    def test_connection_uses_dict_rows_and_preserves_decimal(self):
        connection = connect_postgres(
            POSTGRES_DSN, application_name="NEXUS ERP adapter test"
        )
        try:
            connection.execute(
                "CREATE TEMP TABLE nexus_adapter_decimal_test "
                "(amount NUMERIC(24, 6) NOT NULL)"
            )
            connection.execute(
                "INSERT INTO nexus_adapter_decimal_test(amount) VALUES (%s)",
                (Decimal("1234567890.123456"),),
            )
            row = connection.execute(
                "SELECT amount FROM nexus_adapter_decimal_test"
            ).fetchone()
            self.assertEqual(row["amount"], Decimal("1234567890.123456"))
            self.assertEqual(
                connection.info.parameter_status("application_name"),
                "NEXUS ERP adapter test",
            )
        finally:
            connection.rollback()
            connection.close()

    def test_default_transaction_rolls_back_without_committing(self):
        connection = connect_postgres(POSTGRES_DSN)
        try:
            self.assertFalse(connection.autocommit)
            connection.execute(
                "CREATE TEMP TABLE nexus_adapter_rollback_test "
                "(value TEXT NOT NULL) ON COMMIT PRESERVE ROWS"
            )
            connection.commit()
            connection.execute(
                "INSERT INTO nexus_adapter_rollback_test(value) VALUES (%s)",
                ("must roll back",),
            )
            connection.rollback()
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM nexus_adapter_rollback_test"
            ).fetchone()["count"]
            self.assertEqual(count, 0)
        finally:
            connection.rollback()
            connection.close()

    def test_autocommit_is_explicit_when_requested(self):
        connection = connect_postgres(POSTGRES_DSN, autocommit=True)
        try:
            self.assertTrue(connection.autocommit)
            row = connection.execute("SELECT 1 AS ready").fetchone()
            self.assertEqual(row["ready"], 1)
        finally:
            connection.close()


class PostgreSQLConnectionConfigurationTests(unittest.TestCase):
    def test_missing_dsn_fails_closed_without_sqlite_fallback(self):
        with self.assertRaisesRegex(RuntimeError, "DSN is required"):
            connect_postgres("")

    def test_invalid_timeout_is_rejected_before_connecting(self):
        with self.assertRaisesRegex(ValueError, "between 1 and 30"):
            connect_postgres("postgresql://unused", connect_timeout=0)

    def test_invalid_application_name_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "application_name"):
            connect_postgres("postgresql://unused", application_name="x" * 64)


if __name__ == "__main__":
    unittest.main()
