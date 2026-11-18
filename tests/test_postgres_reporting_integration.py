import os
import unittest
import uuid
from decimal import Decimal

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
        connection = psycopg.connect(
            os.environ["NEXUS_TEST_POSTGRES_DSN"],
            row_factory=psycopg.rows.dict_row,
        )
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



    def test_management_dashboard_ledger_aggregates_are_postgresql_compatible(self):
        from management_dashboard import account_balances, dashboard_period_balances

        schema = "nexus_dashboard_" + uuid.uuid4().hex[:12]
        connection = psycopg.connect(
            os.environ["NEXUS_TEST_POSTGRES_DSN"],
            row_factory=psycopg.rows.dict_row,
        )
        try:
            connection.execute(f'CREATE SCHEMA "{schema}"')
            connection.execute(f'SET search_path TO "{schema}"')
            connection.execute("""CREATE TABLE accounts(
                id BIGINT PRIMARY KEY, code TEXT, name_ar TEXT,
                account_type TEXT, is_group BOOLEAN DEFAULT FALSE
            )""")
            connection.execute("""CREATE TABLE journal_entries(
                id BIGINT PRIMARY KEY, entry_date DATE, status TEXT,
                entry_type TEXT DEFAULT 'general'
            )""")
            connection.execute("""CREATE TABLE journal_lines(
                id BIGINT PRIMARY KEY, entry_id BIGINT, account_id BIGINT,
                debit NUMERIC(20,2), credit NUMERIC(20,2)
            )""")
            connection.execute("INSERT INTO accounts VALUES(1,'1110','Cash','asset',FALSE)")
            connection.execute(
                "INSERT INTO journal_entries VALUES"
                "(1,'2026-01-05','posted','general'),"
                "(2,'2026-02-05','posted','general'),"
                "(3,'2026-02-06','posted','year_end_closing')"
            )
            connection.execute(
                "INSERT INTO journal_lines VALUES"
                "(1,1,1,100.10,0),(2,2,1,25.25,0),(3,3,1,50.00,0)"
            )

            balances = account_balances(
                connection, "2026-02-01", "2026-02-28",
                exclude_closing_entries=True,
            )
            self.assertEqual(balances[1]["debit"], Decimal("25.25"))

            current, previous, as_of, opening = dashboard_period_balances(
                connection, "2026-02-01", "2026-02-28",
                "2026-01-01", "2026-01-31",
                exclude_closing_entries=True,
            )
            self.assertEqual(current[1]["debit"], Decimal("25.25"))
            self.assertEqual(previous[1]["debit"], Decimal("100.10"))
            self.assertEqual(opening[1]["debit"], Decimal("100.10"))
            self.assertEqual(as_of[1]["debit"], Decimal("175.35"))
        finally:
            connection.rollback()
            connection.execute("SET search_path TO public")
            connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            connection.commit()
            connection.close()

if __name__ == "__main__":
    unittest.main()
