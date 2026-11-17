import os
import unittest
import uuid

try:
    import psycopg
except ImportError:
    psycopg = None

from management_reports import build_inventory_ageing


@unittest.skipUnless(
    os.environ.get("NEXUS_TEST_POSTGRES_DSN") and psycopg,
    "PostgreSQL integration DSN/driver is not configured",
)
class ManagementReportsPostgreSQLIntegrationTests(unittest.TestCase):
    def test_inventory_ageing_uses_postgresql_date_expressions(self):
        schema = "nexus_report_" + uuid.uuid4().hex[:12]
        connection = psycopg.connect(\n            os.environ["NEXUS_TEST_POSTGRES_DSN"],\n            row_factory=psycopg.rows.dict_row,\n        )
        try:
            connection.execute(f'CREATE SCHEMA "{schema}"')
            connection.execute(f'SET search_path TO "{schema}"')
            connection.execute("""CREATE TABLE stock_products(
                id BIGINT PRIMARY KEY, sku TEXT, name_ar TEXT
            )""")
            connection.execute("""CREATE TABLE stock_warehouses(
                id BIGINT PRIMARY KEY, code TEXT, name_ar TEXT
            )""")
            connection.execute("""CREATE TABLE stock_movements(
                id BIGINT PRIMARY KEY, product_id BIGINT,
                source_warehouse_id BIGINT, target_warehouse_id BIGINT,
                movement_date DATE
            )""")
            connection.execute("CREATE TABLE stock_lots(id BIGINT PRIMARY KEY, expiry_date DATE)")
            connection.execute("""CREATE TABLE stock_valuation_layers(
                id BIGINT PRIMARY KEY, product_id BIGINT, warehouse_id BIGINT,
                source_movement_id BIGINT, lot_id BIGINT,
                qty_remaining NUMERIC(20,4), unit_cost NUMERIC(20,4),
                created_at TIMESTAMP
            )""")
            connection.execute("INSERT INTO stock_products VALUES(1,'SKU-1','صنف تجريبي')")
            connection.execute("INSERT INTO stock_warehouses VALUES(1,'WH-1','المخزن الرئيسي')")
            connection.execute("INSERT INTO stock_lots VALUES(1,'2026-12-01')")
            connection.execute("INSERT INTO stock_movements VALUES(1,1,1,NULL,'2026-11-10')")
            connection.execute("""INSERT INTO stock_valuation_layers
                VALUES(1,1,1,1,1,2,15,'2026-11-01 08:00:00')""")
            report = build_inventory_ageing(connection, "2026-11-15")
            self.assertTrue(report["available"])
            self.assertEqual(report["total_value"], 30)
            self.assertEqual(report["rows"][0]["last_movement_date"], "2026-11-10")
            self.assertEqual(report["expiry_totals"]["within_30"], 30)
        finally:
            connection.rollback()
            connection.execute("SET search_path TO public")
            connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            connection.commit()
            connection.close()


if __name__ == "__main__":
    unittest.main()
