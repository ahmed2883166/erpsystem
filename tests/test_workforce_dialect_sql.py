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


if __name__ == "__main__":
    unittest.main()
