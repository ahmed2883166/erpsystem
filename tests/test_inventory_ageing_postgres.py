"""PostgreSQL coverage for inventory ageing, isolated in a rollback-only schema."""
import os
import unittest
import uuid
from decimal import Decimal


@unittest.skipUnless(
    os.environ.get("NEXUS_TEST_POSTGRES_DSN"),
    "NEXUS_TEST_POSTGRES_DSN is not configured",
)
class InventoryAgeingPostgreSQLTests(unittest.TestCase):
    def test_inventory_ageing_uses_remaining_layers_and_expiry(self):
        import psycopg
        from psycopg.rows import dict_row
        from management_reports import build_inventory_ageing

        schema = "nexus_inventory_age_" + uuid.uuid4().hex[:12]
        conn = psycopg.connect(
            os.environ["NEXUS_TEST_POSTGRES_DSN"], row_factory=dict_row
        )
        try:
            try:
                with conn.transaction():
                    conn.execute(f'CREATE SCHEMA "{schema}"')
                    conn.execute(f'SET LOCAL search_path TO "{schema}"')
                    conn.execute("""
                        CREATE TABLE stock_products(
                            id BIGINT PRIMARY KEY, sku TEXT, name_ar TEXT
                        )
                    """)
                    conn.execute("""
                        CREATE TABLE stock_warehouses(
                            id BIGINT PRIMARY KEY, code TEXT, name_ar TEXT
                        )
                    """)
                    conn.execute("""
                        CREATE TABLE stock_lots(
                            id BIGINT PRIMARY KEY, expiry_date TEXT
                        )
                    """)
                    conn.execute("""
                        CREATE TABLE stock_movements(
                            id BIGINT PRIMARY KEY, product_id BIGINT,
                            source_warehouse_id BIGINT, target_warehouse_id BIGINT,
                            movement_date TEXT
                        )
                    """)
                    conn.execute("""
                        CREATE TABLE stock_valuation_layers(
                            id BIGINT PRIMARY KEY, product_id BIGINT,
                            warehouse_id BIGINT, source_movement_id BIGINT,
                            lot_id BIGINT, qty_remaining TEXT, unit_cost TEXT,
                            created_at TEXT
                        )
                    """)
                    conn.execute("INSERT INTO stock_products VALUES(1,'P-1','Test product')")
                    conn.execute("INSERT INTO stock_warehouses VALUES(1,'W-1','Main')")
                    conn.execute("""
                        INSERT INTO stock_lots VALUES
                        (1,'2026-11-01'),(2,'2026-12-01')
                    """)
                    conn.execute("""
                        INSERT INTO stock_valuation_layers VALUES
                        (10,1,1,NULL,1,'3','10','2026-07-01 08:00:00'),
                        (11,1,1,NULL,2,'2','20','2026-11-01 08:00:00'),
                        (12,1,1,NULL,NULL,'9','99','2026-11-20 08:00:00')
                    """)
                    conn.execute("""
                        INSERT INTO stock_movements VALUES
                        (10,1,1,NULL,'2026-11-10'),
                        (11,1,1,NULL,'2026-12-10')
                    """)
                    report = build_inventory_ageing(conn, "2026-11-15")
                    self.assertTrue(report["available"])
                    self.assertEqual(len(report["rows"]), 2)
                    self.assertEqual(report["total_value"], Decimal("70"))
                    self.assertEqual(report["expiry_totals"]["expired"], Decimal("30"))
                    self.assertEqual(report["expiry_totals"]["within_30"], Decimal("40"))
                    self.assertTrue(all(
                        row["last_movement_date"] == "2026-11-10"
                        for row in report["rows"]
                    ))
                    raise RuntimeError("rollback disposable schema")
            except RuntimeError as exc:
                self.assertEqual(str(exc), "rollback disposable schema")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
