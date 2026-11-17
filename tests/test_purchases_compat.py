"""Regression coverage for serialized purchase-order receiving."""
import os
import sqlite3
import unittest

from purchases import (
    _purchase_order_for_receipt_update,
    _purchase_order_lines_for_receipt_update,
)


class SQLitePurchaseReceiptLockTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE purchase_orders (
                id INTEGER PRIMARY KEY, supplier_id INTEGER NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE purchase_order_lines (
                id INTEGER PRIMARY KEY, po_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL, quantity TEXT NOT NULL,
                received_qty TEXT NOT NULL
            );
            INSERT INTO purchase_orders VALUES (11, 4, 'approved');
            INSERT INTO purchase_order_lines VALUES (21, 11, 8, '10', '3');
            """
        )

    def tearDown(self):
        self.connection.close()

    def test_receipt_reads_order_and_line_inside_sqlite_transaction(self):
        order = _purchase_order_for_receipt_update(self.connection, 11)
        lines = _purchase_order_lines_for_receipt_update(self.connection, 11, 8)
        self.assertEqual(order["supplier_id"], 4)
        self.assertEqual(order["status"], "approved")
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["received_qty"], "3")


class PostgreSQLPurchaseReceiptLockTests(unittest.TestCase):
    def test_postgresql_queries_lock_order_header_and_matching_lines(self):
        class Cursor:
            def __init__(self, row=None, rows=None):
                self.row = row
                self.rows = rows or []

            def fetchone(self):
                return self.row

            def fetchall(self):
                return self.rows

        class Connection:
            __module__ = "psycopg.fake"

            def __init__(self):
                self.calls = []

            def execute(self, statement, params=()):
                self.calls.append((statement, tuple(params)))
                if "purchase_orders" in statement:
                    return Cursor({"id": 11, "supplier_id": 4, "status": "approved"})
                return Cursor(rows=[{"id": 21, "received_qty": "3"}])

        connection = Connection()
        order = _purchase_order_for_receipt_update(connection, 11)
        lines = _purchase_order_lines_for_receipt_update(connection, 11, 8)

        self.assertEqual(order["id"], 11)
        self.assertEqual(lines[0]["id"], 21)
        self.assertIn("FOR UPDATE", connection.calls[0][0])
        self.assertIn("ORDER BY id FOR UPDATE", connection.calls[1][0])
        self.assertEqual(connection.calls, [
            (connection.calls[0][0], (11,)),
            (connection.calls[1][0], (11, 8)),
        ])


POSTGRES_DSN = os.environ.get("NEXUS_TEST_POSTGRES_DSN", "").strip()


@unittest.skipUnless(POSTGRES_DSN, "NEXUS_TEST_POSTGRES_DSN is not configured")
class PostgreSQLPurchaseReceiptIntegrationTests(unittest.TestCase):
    def setUp(self):
        from postgres_database import connect_postgres

        self.connection = connect_postgres(POSTGRES_DSN)
        self.connection.execute(
            "CREATE TEMP TABLE purchase_orders ("
            "id BIGINT PRIMARY KEY, supplier_id BIGINT NOT NULL, status TEXT NOT NULL)"
        )
        self.connection.execute(
            "CREATE TEMP TABLE purchase_order_lines ("
            "id BIGINT PRIMARY KEY, po_id BIGINT NOT NULL, product_id BIGINT NOT NULL, "
            "quantity TEXT NOT NULL, received_qty TEXT NOT NULL)"
        )
        self.connection.execute(
            "INSERT INTO purchase_orders VALUES (11, 4, 'approved')"
        )
        self.connection.execute(
            "INSERT INTO purchase_order_lines VALUES (21, 11, 8, '10', '3')"
        )

    def tearDown(self):
        self.connection.rollback()
        self.connection.close()

    def test_postgresql_accepts_locked_receipt_queries(self):
        order = _purchase_order_for_receipt_update(self.connection, 11)
        lines = _purchase_order_lines_for_receipt_update(self.connection, 11, 8)
        self.assertEqual(order["supplier_id"], 4)
        self.assertEqual(order["status"], "approved")
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["received_qty"], "3")


if __name__ == "__main__":
    unittest.main()
