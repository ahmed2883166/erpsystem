"""Focused regression tests for the inventory transaction and valuation core."""
import sqlite3
import unittest
from decimal import Decimal

from accounting_core import register_decimal_aggregates
from stock import current_qty, consume_layers, lock_stock_warehouses


class SQLiteStockCoreTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        register_decimal_aggregates(self.connection)
        self.connection.executescript(
            """
            CREATE TABLE stock_warehouses (id INTEGER PRIMARY KEY);
            INSERT INTO stock_warehouses(id) VALUES (1), (2);
            CREATE TABLE stock_movements (
                id INTEGER PRIMARY KEY,
                movement_type TEXT NOT NULL,
                product_id INTEGER NOT NULL,
                source_warehouse_id INTEGER,
                target_warehouse_id INTEGER,
                lot_id INTEGER,
                quantity TEXT NOT NULL
            );
            CREATE TABLE stock_valuation_layers (
                id INTEGER PRIMARY KEY,
                product_id INTEGER NOT NULL,
                warehouse_id INTEGER NOT NULL,
                location_id INTEGER,
                lot_id INTEGER,
                receipt_line_id INTEGER,
                qty_in TEXT NOT NULL,
                qty_remaining TEXT NOT NULL,
                unit_cost TEXT NOT NULL,
                value_remaining TEXT NOT NULL
            );
            """
        )

    def tearDown(self):
        self.connection.close()

    def test_current_quantity_uses_decimal_strings_and_transfer_direction(self):
        self.connection.executemany(
            """INSERT INTO stock_movements
               (movement_type,product_id,source_warehouse_id,target_warehouse_id,lot_id,quantity)
               VALUES(?,?,?,?,?,?)""",
            [
                ("receipt", 7, None, 1, None, "10.1250"),
                ("issue", 7, 1, None, None, "2.0000"),
                ("scrap", 7, 1, None, None, "0.1250"),
                ("transfer", 7, 1, 2, None, "3.5000"),
                ("relocate", 7, 1, 1, None, "1.0000"),
            ],
        )
        self.assertEqual(current_qty(self.connection, 7, 1), Decimal("4.5000"))
        self.assertEqual(current_qty(self.connection, 7, 2), Decimal("3.5000"))

    def _seed_layers(self):
        self.connection.executemany(
            """INSERT INTO stock_valuation_layers
               (product_id,warehouse_id,qty_in,qty_remaining,unit_cost,value_remaining)
               VALUES(7,1,?,?,?,?)""",
            [("2", "2", "10", "20"), ("3", "3", "12", "36")],
        )

    def test_fifo_consumption_preserves_exact_remaining_layer_value(self):
        self._seed_layers()
        cost, allocations = consume_layers(
            self.connection, 7, 1, Decimal("2.5000"), "fifo"
        )
        self.assertEqual(cost, Decimal("26.00"))
        self.assertEqual(sum((item[1] for item in allocations), Decimal("0")), Decimal("2.5000"))
        rows = self.connection.execute(
            "SELECT qty_remaining,value_remaining FROM stock_valuation_layers ORDER BY id"
        ).fetchall()
        self.assertEqual((rows[0]["qty_remaining"], rows[0]["value_remaining"]), ("0.0000", "0"))
        self.assertEqual((rows[1]["qty_remaining"], rows[1]["value_remaining"]), ("2.5000", "30.00"))

    def test_average_consumption_rebases_remaining_inventory_without_value_drift(self):
        self._seed_layers()
        cost, _ = consume_layers(
            self.connection, 7, 1, Decimal("2.5000"), "average"
        )
        self.assertEqual(cost, Decimal("28.00"))
        row = self.connection.execute(
            "SELECT SUM(CAST(qty_remaining AS NUMERIC)) qty, "
            "SUM(CAST(value_remaining AS NUMERIC)) value FROM stock_valuation_layers"
        ).fetchone()
        self.assertEqual(Decimal(str(row["qty"])), Decimal("2.5"))
        self.assertEqual(Decimal(str(row["value"])), Decimal("28.00"))

    def test_insufficient_stock_does_not_mutate_valuation_layers(self):
        self._seed_layers()
        before = [tuple(row) for row in self.connection.execute(
            "SELECT qty_remaining,value_remaining FROM stock_valuation_layers ORDER BY id"
        )]
        with self.assertRaisesRegex(ValueError, "الرصيد غير كافٍ"):
            consume_layers(self.connection, 7, 1, Decimal("5.0001"), "fifo")
        after = [tuple(row) for row in self.connection.execute(
            "SELECT qty_remaining,value_remaining FROM stock_valuation_layers ORDER BY id"
        )]
        self.assertEqual(after, before)

    def test_sqlite_warehouse_lock_is_left_to_begin_immediate_transaction(self):
        self.assertEqual(lock_stock_warehouses(self.connection, 2, 1, 2), [])


class PostgreSQLWarehouseLockTests(unittest.TestCase):
    def test_locks_unique_warehouses_in_stable_order(self):
        class Cursor:
            def fetchall(self):
                return [{"id": 2}, {"id": 5}]

        class Connection:
            __module__ = "psycopg.fake"

            def __init__(self):
                self.calls = []

            def execute(self, statement, params=()):
                self.calls.append((statement, tuple(params)))
                return Cursor()

        connection = Connection()
        rows = lock_stock_warehouses(connection, 5, 2, 5)
        self.assertEqual([row["id"] for row in rows], [2, 5])
        statement, params = connection.calls[0]
        self.assertIn("ORDER BY id FOR UPDATE", statement)
        self.assertEqual(params, (2, 5))

    def test_missing_warehouse_fails_closed(self):
        class Cursor:
            def fetchall(self):
                return [{"id": 2}]

        class Connection:
            __module__ = "psycopg.fake"

            def execute(self, statement, params=()):
                return Cursor()

        with self.assertRaisesRegex(ValueError, "المخزن المحدد غير موجود"):
            lock_stock_warehouses(Connection(), 2, 5)




POSTGRES_DSN = __import__("os").environ.get("NEXUS_TEST_POSTGRES_DSN", "").strip()


@unittest.skipUnless(POSTGRES_DSN, "NEXUS_TEST_POSTGRES_DSN is not configured")
class PostgreSQLStockCoreTests(unittest.TestCase):
    def setUp(self):
        import uuid
        from postgres_database import connect_postgres

        self.connection = connect_postgres(POSTGRES_DSN)
        self.schema = "nexus_stock_test_" + uuid.uuid4().hex
        self.connection.execute(f'CREATE SCHEMA "{self.schema}"')
        self.connection.execute(f'SET LOCAL search_path TO "{self.schema}"')
        self.connection.execute("CREATE TABLE stock_warehouses (id BIGINT PRIMARY KEY)")
        self.connection.execute(
            """CREATE TABLE stock_valuation_layers (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                product_id BIGINT NOT NULL, warehouse_id BIGINT NOT NULL,
                location_id BIGINT, lot_id BIGINT, receipt_line_id BIGINT,
                qty_in TEXT NOT NULL, qty_remaining TEXT NOT NULL,
                unit_cost TEXT NOT NULL, value_remaining TEXT NOT NULL
            )"""
        )
        self.connection.execute(
            """CREATE TABLE stock_movements (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                movement_type TEXT NOT NULL, product_id BIGINT NOT NULL,
                source_warehouse_id BIGINT, target_warehouse_id BIGINT,
                lot_id BIGINT, quantity TEXT NOT NULL
            )"""
        )
        self.connection.execute("INSERT INTO stock_warehouses(id) VALUES (1), (2)")
        self.connection.execute(
            """INSERT INTO stock_valuation_layers
               (product_id,warehouse_id,qty_in,qty_remaining,unit_cost,value_remaining)
               VALUES (7,1,'2','2','10','20'), (7,1,'3','3','12','36')"""
        )

    def tearDown(self):
        self.connection.rollback()
        self.connection.close()

    def test_postgresql_locks_stock_warehouses_and_consumes_fifo_layers(self):
        locked = lock_stock_warehouses(self.connection, 2, 1, 2)
        self.assertEqual([row["id"] for row in locked], [1, 2])
        cost, _ = consume_layers(
            self.connection, 7, 1, Decimal("2.5000"), "fifo"
        )
        self.assertEqual(cost, Decimal("26.00"))
        rows = self.connection.execute(
            "SELECT qty_remaining,value_remaining FROM stock_valuation_layers ORDER BY id"
        ).fetchall()
        self.assertEqual((rows[0]["qty_remaining"], rows[0]["value_remaining"]), ("0.0000", "0"))
        self.assertEqual((rows[1]["qty_remaining"], rows[1]["value_remaining"]), ("2.5000", "30.00"))

    def test_postgresql_current_quantity_keeps_decimal_precision(self):
        self.connection.execute(
            """INSERT INTO stock_movements
               (movement_type,product_id,target_warehouse_id,quantity)
               VALUES ('receipt',7,1,'10.1250')"""
        )
        self.connection.execute(
            """INSERT INTO stock_movements
               (movement_type,product_id,source_warehouse_id,quantity)
               VALUES ('issue',7,1,'0.1250')"""
        )
        self.assertEqual(
            current_qty(self.connection, 7, 1), Decimal("10.0000")
        )


if __name__ == "__main__":
    unittest.main()
