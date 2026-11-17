"""Stock module database-neutral metadata helper tests."""
import os
import sqlite3
import unittest
from decimal import Decimal

from stock import _columns, _table_exists


class StockMetadataSQLiteTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE stock_probe (id INTEGER PRIMARY KEY, sku TEXT)")

    def tearDown(self):
        self.conn.close()

    def test_table_exists(self):
        self.assertTrue(_table_exists(self.conn, "stock_probe"))
        self.assertFalse(_table_exists(self.conn, "missing_probe"))

    def test_columns(self):
        self.assertEqual(_columns(self.conn, "stock_probe"), {"id", "sku"})
        self.assertEqual(_columns(self.conn, "missing_probe"), set())


@unittest.skipUnless(os.environ.get("NEXUS_TEST_POSTGRES_DSN"), "PostgreSQL integration DSN not configured")
class StockMetadataPostgreSQLTests(unittest.TestCase):
    def test_metadata_helpers_in_rolled_back_schema(self):
        import uuid
        import psycopg
        from psycopg.rows import dict_row

        schema = "nexus_stock_meta_" + uuid.uuid4().hex[:12]
        conn = psycopg.connect(os.environ["NEXUS_TEST_POSTGRES_DSN"], row_factory=dict_row)
        try:
            try:
                with conn.transaction():
                    conn.execute(f'CREATE SCHEMA "{schema}"')
                    conn.execute(f'SET LOCAL search_path TO "{schema}"')
                    conn.execute("CREATE TABLE stock_probe (id BIGINT PRIMARY KEY, sku TEXT)")
                    self.assertTrue(_table_exists(conn, "stock_probe"))
                    self.assertFalse(_table_exists(conn, "missing_probe"))
                    self.assertEqual(_columns(conn, "stock_probe"), {"id", "sku"})
                    self.assertEqual(_columns(conn, "missing_probe"), set())
                    raise RuntimeError("rollback disposable schema")
            except RuntimeError as exc:
                self.assertEqual(str(exc), "rollback disposable schema")
        finally:
            conn.close()

    def test_postgresql_issue_consumes_locked_valuation_layer(self):
        import uuid
        import psycopg
        from psycopg.rows import dict_row
        from stock import consume_layers

        schema = "nexus_stock_issue_" + uuid.uuid4().hex[:12]
        conn = psycopg.connect(os.environ["NEXUS_TEST_POSTGRES_DSN"], row_factory=dict_row)
        try:
            try:
                with conn.transaction():
                    conn.execute(f'CREATE SCHEMA "{schema}"')
                    conn.execute(f'SET LOCAL search_path TO "{schema}"')
                    conn.execute("""
                        CREATE TABLE stock_warehouses (
                            id BIGINT PRIMARY KEY
                        )
                    """)
                    conn.execute("INSERT INTO stock_warehouses(id) VALUES (3)")
                    conn.execute("""
                        CREATE TABLE stock_valuation_layers (
                            id BIGINT PRIMARY KEY,
                            product_id BIGINT NOT NULL,
                            warehouse_id BIGINT NOT NULL,
                            lot_id BIGINT,
                            location_id BIGINT,
                            qty_remaining NUMERIC(24,8) NOT NULL,
                            value_remaining NUMERIC(24,8) NOT NULL,
                            unit_cost NUMERIC(24,8) NOT NULL
                        )
                    """)
                    conn.execute("""
                        INSERT INTO stock_valuation_layers
                        (id,product_id,warehouse_id,lot_id,location_id,qty_remaining,value_remaining,unit_cost)
                        VALUES (1,12,3,NULL,NULL,5,50,10)
                    """)
                    cost, allocations = consume_layers(conn, 12, 3, Decimal("2"), "fifo")
                    self.assertEqual(cost, Decimal("20.00"))
                    self.assertEqual(allocations[0][1], Decimal("2"))
                    remaining = conn.execute(
                        "SELECT qty_remaining,value_remaining FROM stock_valuation_layers WHERE id=1"
                    ).fetchone()
                    self.assertEqual(remaining["qty_remaining"], Decimal("3.00000000"))
                    self.assertEqual(remaining["value_remaining"], Decimal("30.00000000"))
                    raise RuntimeError("rollback disposable stock issue")
            except RuntimeError as exc:
                self.assertEqual(str(exc), "rollback disposable stock issue")
        finally:
            conn.close()




class StockLocationTriggerDialectTests(unittest.TestCase):
    def test_sqlite_installs_location_integrity_triggers(self):
        from stock import _install_stock_location_triggers

        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript("""
                CREATE TABLE stock_locations(id INTEGER PRIMARY KEY, warehouse_id INTEGER, active INTEGER);
                CREATE TABLE stock_product_locations(
                    id INTEGER PRIMARY KEY, product_id INTEGER, warehouse_id INTEGER, location_id INTEGER
                );
            """)
            _install_stock_location_triggers(conn)
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
            self.assertEqual(names, {
                "trg_stock_product_locations_same_wh_insert",
                "trg_stock_product_locations_same_wh_update",
                "trg_stock_locations_prevent_default_deactivation",
            })
            conn.execute("INSERT INTO stock_locations(id,warehouse_id,active) VALUES(1,10,1)")
            conn.execute("INSERT INTO stock_product_locations(id,product_id,warehouse_id,location_id) VALUES(1,20,10,1)")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "لا يمكن تعطيل موقع"):
                conn.execute("UPDATE stock_locations SET active=0 WHERE id=1")
            conn.execute("DELETE FROM stock_product_locations WHERE id=1")
            conn.execute("UPDATE stock_locations SET active=0 WHERE id=1")
            self.assertEqual(conn.execute("SELECT active FROM stock_locations WHERE id=1").fetchone()[0], 0)
        finally:
            conn.close()

    def test_postgresql_skips_sqlite_raise_trigger_ddl(self):
        from stock import _install_stock_location_triggers

        class FakePostgresConnection:
            __module__ = "psycopg"
            def execute(self, *args, **kwargs):
                raise AssertionError("SQLite trigger SQL must not run on PostgreSQL")

        _install_stock_location_triggers(FakePostgresConnection())


class StockWarehouseLockTests(unittest.TestCase):
    def test_postgresql_locks_pending_movement_during_resolution(self):
        from stock import _stock_movement_for_update

        class Cursor:
            def fetchone(self):
                return {"id": 9, "accounting_status": "pending_setup"}

        class FakePostgresConnection:
            __module__ = "psycopg"

            def __init__(self):
                self.sql = None
                self.params = None

            def execute(self, sql, params=()):
                self.sql = sql
                self.params = params
                return Cursor()

        conn = FakePostgresConnection()
        row = _stock_movement_for_update(conn, 9)
        self.assertEqual(row["id"], 9)
        self.assertIn("FOR UPDATE OF m", conn.sql)
        self.assertEqual(tuple(conn.params), (9,))

    def test_postgresql_locks_warehouses_in_stable_order(self):
        from stock import lock_stock_warehouses

        class Cursor:
            def fetchall(self):
                return [{"id": 3}, {"id": 12}]

        class FakePostgresConnection:
            __module__ = "psycopg"

            def __init__(self):
                self.sql = None
                self.params = None

            def execute(self, sql, params=()):
                self.sql = sql
                self.params = params
                return Cursor()

        conn = FakePostgresConnection()
        rows = lock_stock_warehouses(conn, 12, 3, 12, None)
        self.assertEqual([row["id"] for row in rows], [3, 12])
        self.assertIn("ORDER BY id FOR UPDATE", conn.sql)
        self.assertEqual(tuple(conn.params), (3, 12))

    def test_sqlite_warehouse_lock_uses_existing_writer_serialization(self):
        from stock import lock_stock_warehouses

        conn = sqlite3.connect(":memory:")
        try:
            self.assertEqual(lock_stock_warehouses(conn, 12, 3), [])
        finally:
            conn.close()


class StockNullSafeComparisonTests(unittest.TestCase):
    def test_null_lot_comparison_is_portable_and_matches_unassigned_lot(self):
        from db_sql import execute_compat, qmark_to_format

        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE TABLE stock_movements (lot_id INTEGER, quantity TEXT)")
            conn.executemany(
                "INSERT INTO stock_movements(lot_id,quantity) VALUES(?,?)",
                [(None, "4"), (7, "3")],
            )
            predicate = "(lot_id = ? OR (lot_id IS NULL AND ? IS NULL))"
            row = execute_compat(
                conn, "SELECT SUM(CAST(quantity AS INTEGER)) total FROM stock_movements WHERE " + predicate,
                (None, None),
            ).fetchone()
            self.assertEqual(row[0], 4)
            self.assertEqual(
                qmark_to_format("SELECT 1 WHERE " + predicate),
                "SELECT 1 WHERE (lot_id = %s OR (lot_id IS NULL AND %s IS NULL))",
            )
        finally:
            conn.close()

    def test_null_parent_comparison_is_portable(self):
        from db_sql import execute_compat, qmark_to_format

        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE TABLE stock_locations (warehouse_id INTEGER, parent_id INTEGER, code TEXT)")
            conn.execute("INSERT INTO stock_locations VALUES(1,NULL,'FLOOR')")
            predicate = "(parent_id = ? OR (parent_id IS NULL AND ? IS NULL))"
            row = execute_compat(
                conn, "SELECT 1 FROM stock_locations WHERE warehouse_id=? AND " + predicate + " AND lower(code)=lower(?)",
                (1, None, None, "floor"),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertIn("parent_id IS NULL", qmark_to_format(predicate))
        finally:
            conn.close()


class StockValuationLayerLockTests(unittest.TestCase):
    def test_postgresql_consumption_locks_valuation_layers(self):
        from stock import available_layers

        class Cursor:
            def fetchall(self):
                return []

        class FakePostgresConnection:
            __module__ = "psycopg"
            def __init__(self):
                self.sql = None
                self.params = None
            def execute(self, sql, params=()):
                self.sql = sql
                self.params = params
                return Cursor()

        conn = FakePostgresConnection()
        self.assertEqual(available_layers(conn, 12, 3), [])
        self.assertIn("ORDER BY id FOR UPDATE", conn.sql)
        self.assertEqual(tuple(conn.params), (12, 3))

    def test_sqlite_layer_query_does_not_add_postgresql_lock_clause(self):
        from stock import available_layers

        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("""
                CREATE TABLE stock_valuation_layers (
                    id INTEGER PRIMARY KEY, product_id INTEGER, warehouse_id INTEGER,
                    qty_remaining TEXT
                )
            """)
            conn.create_function(
                "DECIMAL_CMP", 2,
                lambda left, right: (Decimal(str(left or "0")) > Decimal(str(right or "0")))
                - (Decimal(str(left or "0")) < Decimal(str(right or "0"))),
            )
            self.assertEqual(available_layers(conn, 12, 3), [])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
