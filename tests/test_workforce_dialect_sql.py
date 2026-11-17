import os
import sqlite3
import unittest

import workforce


class FakePostgresConnection:
    __module__ = "psycopg"


class WorkforceDialectSQLTests(unittest.TestCase):
    def test_group_concat_expression_is_dialect_specific(self):
        sqlite_connection = sqlite3.connect(":memory:")
        try:
            self.assertEqual(
                workforce._group_concat_expression(sqlite_connection, "employee_id"),
                "GROUP_CONCAT(employee_id, ', ')",
            )
        finally:
            sqlite_connection.close()

        self.assertEqual(
            workforce._group_concat_expression(FakePostgresConnection(), "employee_id"),
            "STRING_AGG(CAST(employee_id AS TEXT), ', ')",
        )

    def test_friday_predicate_is_dialect_specific(self):
        sqlite_connection = sqlite3.connect(":memory:")
        try:
            self.assertEqual(
                workforce._friday_attendance_predicate(sqlite_connection),
                "strftime('%w',a.attendance_date)='5'",
            )
        finally:
            sqlite_connection.close()

        self.assertEqual(
            workforce._friday_attendance_predicate(FakePostgresConnection()),
            "EXTRACT(DOW FROM CAST(a.attendance_date AS DATE))=5",
        )


POSTGRES_DSN = os.environ.get("NEXUS_TEST_POSTGRES_DSN", "").strip()


@unittest.skipUnless(POSTGRES_DSN, "NEXUS_TEST_POSTGRES_DSN is not configured")
class WorkforcePostgreSQLDialectIntegrationTests(unittest.TestCase):
    """Execute workforce SQL fragments against a real PostgreSQL connection."""

    def test_friday_filter_and_employee_aggregation_execute_on_postgresql(self):
        from postgres_database import connect_postgres

        connection = connect_postgres(
            POSTGRES_DSN, application_name="NEXUS workforce dialect integration test"
        )
        try:
            connection.execute("""
                CREATE TEMP TABLE attendance_records (
                    employee_id BIGINT NOT NULL,
                    attendance_date TEXT NOT NULL
                )
            """)
            connection.executemany(
                "INSERT INTO attendance_records(employee_id,attendance_date) VALUES (%s,%s)",
                [
                    (10, "2026-11-20"),  # Friday
                    (11, "2026-11-19"),  # Thursday
                    (12, "2026-11-27"),  # Friday
                ],
            )
            expression = workforce._group_concat_expression(connection, "a.employee_id")
            friday = workforce._friday_attendance_predicate(connection)
            row = connection.execute(
                f"SELECT {expression} AS employee_ids FROM attendance_records a WHERE {friday}"
            ).fetchone()
            self.assertEqual(set(row["employee_ids"].split(", ")), {"10", "12"})
        finally:
            connection.rollback()
            connection.close()


if __name__ == "__main__":
    unittest.main()
