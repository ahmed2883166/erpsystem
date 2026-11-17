"""PostgreSQL integration coverage for profitability reports and exact money totals."""
import os
import unittest
import uuid
from decimal import Decimal


@unittest.skipUnless(
    os.environ.get("NEXUS_TEST_POSTGRES_DSN"),
    "NEXUS_TEST_POSTGRES_DSN is not configured",
)
class ProfitabilityPostgreSQLTests(unittest.TestCase):
    def test_branch_warehouse_and_product_profitability(self):
        import psycopg
        from psycopg.rows import dict_row
        from profitability_reports import build_profitability_report

        schema = "nexus_profit_" + uuid.uuid4().hex[:12]
        conn = psycopg.connect(
            os.environ["NEXUS_TEST_POSTGRES_DSN"], row_factory=dict_row
        )
        try:
            try:
                with conn.transaction():
                    conn.execute(f'CREATE SCHEMA "{schema}"')
                    conn.execute(f'SET LOCAL search_path TO "{schema}"')
                    ddl = [
                        """CREATE TABLE accounts(
                            id BIGINT PRIMARY KEY, code TEXT, name_ar TEXT,
                            account_type TEXT, is_group INTEGER,
                            statement_section TEXT)""",
                        """CREATE TABLE journal_entries(
                            id BIGINT PRIMARY KEY, entry_date TEXT,
                            status TEXT, entry_type TEXT)""",
                        """CREATE TABLE journal_lines(
                            id BIGINT PRIMARY KEY, entry_id BIGINT, account_id BIGINT,
                            debit TEXT, credit TEXT, branch_id BIGINT, cost_center_id BIGINT)""",
                        "CREATE TABLE branches(id BIGINT PRIMARY KEY,code TEXT,name_ar TEXT)",
                        "CREATE TABLE cost_centers(id BIGINT PRIMARY KEY,code TEXT,name_ar TEXT)",
                        "CREATE TABLE management_account_roles(account_id BIGINT,role TEXT)",
                        "CREATE TABLE stock_warehouses(id BIGINT PRIMARY KEY,code TEXT,name_ar TEXT)",
                        "CREATE TABLE stock_products(id BIGINT PRIMARY KEY,sku TEXT,name_ar TEXT)",
                        """CREATE TABLE sales_invoices(
                            id BIGINT PRIMARY KEY,invoice_date TEXT,status TEXT,
                            warehouse_id BIGINT,subtotal TEXT,discount_amount TEXT)""",
                        """CREATE TABLE sales_invoice_lines(
                            id BIGINT PRIMARY KEY,invoice_id BIGINT,cost_total TEXT,
                            product_id BIGINT,description TEXT,quantity TEXT,line_total TEXT)""",
                        """CREATE TABLE sales_credit_notes(
                            id BIGINT PRIMARY KEY,invoice_id BIGINT,credit_date TEXT,
                            status TEXT,subtotal TEXT)""",
                        """CREATE TABLE sales_credit_note_lines(
                            id BIGINT PRIMARY KEY,credit_note_id BIGINT,cost_total TEXT,
                            product_id BIGINT,description TEXT,quantity TEXT,line_total TEXT)""",
                    ]
                    for statement in ddl:
                        conn.execute(statement)
                    conn.cursor().executemany(
                        "INSERT INTO accounts VALUES(%s,%s,%s,%s,%s,%s)",
                        [
                            (1, "4100", "إيرادات", "revenue", 0, "revenue"),
                            (2, "5100", "تكلفة المبيعات", "expense", 0, "direct_costs"),
                            (3, "6100", "مصروفات تشغيل", "expense", 0, "operating_expenses"),
                        ],
                    )
                    conn.execute("INSERT INTO branches VALUES(1,'B1','فرع أول')")
                    conn.execute("INSERT INTO cost_centers VALUES(1,'CC1','نشاط التجارة')")
                    conn.execute("INSERT INTO management_account_roles VALUES(2,'direct_cost')")
                    conn.execute("INSERT INTO stock_warehouses VALUES(1,'W1','مخزن رئيسي')")
                    conn.execute("INSERT INTO stock_products VALUES(1,'P1','منتج أول')")
                    conn.execute("INSERT INTO journal_entries VALUES(1,'2026-11-10','posted','general')")
                    conn.cursor().executemany(
                        "INSERT INTO journal_lines VALUES(%s,1,%s,%s,%s,1,1)",
                        [
                            (1, 1, "0", "0.10"),
                            (2, 1, "0", "0.20"),
                            (3, 2, "0.05", "0"),
                            (4, 3, "0.02", "0"),
                        ],
                    )
                    conn.execute(
                        "INSERT INTO sales_invoices VALUES(1,'2026-11-10','posted',1,'100','10')"
                    )
                    conn.execute(
                        "INSERT INTO sales_invoice_lines VALUES(1,1,'30',1,'منتج أول','2','100')"
                    )
                    conn.execute(
                        "INSERT INTO sales_credit_notes VALUES(1,1,'2026-11-15','posted','10')"
                    )
                    conn.execute(
                        "INSERT INTO sales_credit_note_lines VALUES(1,1,'3',1,'منتج أول','1','10')"
                    )

                    report = build_profitability_report(
                        conn, "2026-11-01", "2026-11-30"
                    )
                    branch = report["branches"][0]
                    self.assertEqual(branch["revenue"], Decimal("0.30"))
                    self.assertEqual(branch["cogs"], Decimal("0.05"))
                    self.assertEqual(branch["operating_expenses"], Decimal("0.02"))
                    self.assertEqual(branch["net_profit"], Decimal("0.23"))

                    warehouse = report["warehouses"][0]
                    self.assertEqual(warehouse["revenue"], Decimal("90"))
                    self.assertEqual(warehouse["cogs"], Decimal("27"))
                    self.assertEqual(warehouse["gross_profit"], Decimal("63"))

                    product = report["products"][0]
                    self.assertEqual(product["revenue"], Decimal("80"))
                    self.assertEqual(product["cogs"], Decimal("27"))
                    self.assertEqual(product["gross_profit"], Decimal("53"))
                    raise RuntimeError("rollback disposable schema")
            except RuntimeError as exc:
                self.assertEqual(str(exc), "rollback disposable schema")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
