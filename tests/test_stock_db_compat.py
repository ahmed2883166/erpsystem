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
            })
        finally:
            conn.close()

    def test_postgresql_skips_sqlite_raise_trigger_ddl(self):
        from stock import _install_stock_location_triggers

        class FakePostgresConnection:
            __module__ = "psycopg"
            def execute(self, *args, **kwargs):
                raise AssertionError("SQLite trigger SQL must not run on PostgreSQL")

        _install_stock_location_triggers(FakePostgresConnection())


if __name__ == "__main__":
    unittest.main()


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
        self.assertEqual(conn.params, (12, 3))

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
