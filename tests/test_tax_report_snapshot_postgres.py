"""PostgreSQL integration coverage for the monthly VAT report snapshot.

All objects are created in a disposable schema and rolled back.
"""
import os
import unittest
import uuid
from decimal import Decimal


@unittest.skipUnless(
    os.environ.get("NEXUS_TEST_POSTGRES_DSN"),
    "NEXUS_TEST_POSTGRES_DSN is not configured",
)
class TaxReportSnapshotPostgreSQLTests(unittest.TestCase):
    def test_monthly_snapshot_reads_sales_purchases_and_company_settings(self):
        import psycopg
        from psycopg.rows import dict_row
        from tax_center import build_tax_report_snapshot

        schema = "nexus_tax_snapshot_" + uuid.uuid4().hex[:12]
        conn = psycopg.connect(
            os.environ["NEXUS_TEST_POSTGRES_DSN"], row_factory=dict_row
        )
        try:
            try:
                with conn.transaction():
                    conn.execute(f'CREATE SCHEMA "{schema}"')
                    conn.execute(f'SET LOCAL search_path TO "{schema}"')
                    conn.execute("""
                        CREATE TABLE company_settings(
                            setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL
                        )
                    """)
                    conn.execute("""
                        INSERT INTO company_settings VALUES
                        ('company_name','NEXUS Test'),
                        ('tax_country','EG'),
                        ('tax_number','123456789'),
                        ('activity','commercial')
                    """)
                    conn.execute("""
                        CREATE TABLE customers(
                            id BIGINT PRIMARY KEY, name TEXT NOT NULL, tax_number TEXT
                        )
                    """)
                    conn.execute("""
                        CREATE TABLE suppliers(
                            id BIGINT PRIMARY KEY, name TEXT NOT NULL, tax_number TEXT
                        )
                    """)
                    conn.execute("""
                        CREATE TABLE sales_invoices(
                            id BIGINT PRIMARY KEY, invoice_no TEXT, customer_id BIGINT,
                            invoice_date TEXT, subtotal TEXT, tax_rate TEXT,
                            tax_amount TEXT, total_amount TEXT, status TEXT,
                            tax_treatment TEXT, tax_reference TEXT, tax_notes TEXT
                        )
                    """)
                    conn.execute("""
                        CREATE TABLE supplier_bills(
                            id BIGINT PRIMARY KEY, bill_no TEXT, supplier_id BIGINT,
                            bill_date TEXT, subtotal TEXT, tax_rate TEXT,
                            tax_amount TEXT, total_amount TEXT, status TEXT,
                            tax_treatment TEXT, tax_reference TEXT, tax_notes TEXT,
                            input_vat_deductible_percent TEXT
                        )
                    """)
                    conn.execute("INSERT INTO customers VALUES(1,'Customer A','123456789')")
                    conn.execute("INSERT INTO suppliers VALUES(1,'Supplier A','987654321')")
                    conn.execute("""
                        INSERT INTO sales_invoices VALUES
                        (1,'SI-PG-1',1,'2026-11-05','1000','14','140','1140',
                         'posted','standard','EG-INV-1','')
                    """)
                    conn.execute("""
                        INSERT INTO supplier_bills VALUES
                        (1,'PB-PG-1',1,'2026-11-07','400','14','56','456',
                         'posted','standard','EG-BILL-1','', '100')
                    """)
                    conn.execute("""
                        CREATE TABLE accounts(
                            id BIGINT PRIMARY KEY, code TEXT, name_ar TEXT,
                            account_type TEXT
                        )
                    """)
                    conn.execute("""
                        CREATE TABLE journal_entries(
                            id BIGINT PRIMARY KEY, entry_date TEXT, status TEXT
                        )
                    """)
                    conn.execute("""
                        CREATE TABLE journal_lines(
                            id BIGINT PRIMARY KEY, entry_id BIGINT, account_id BIGINT,
                            debit TEXT, credit TEXT
                        )
                    """)
                    conn.execute("""
                        INSERT INTO accounts VALUES
                        (1,'2130','Output VAT','liability'),
                        (2,'1165','Input VAT','asset')
                    """)
                    conn.execute("""
                        INSERT INTO journal_entries VALUES
                        (1,'2026-11-05','posted'),(2,'2026-11-07','posted')
                    """)
                    conn.execute("""
                        INSERT INTO journal_lines VALUES
                        (1,1,1,'0','140'),(2,2,2,'56','0')
                    """)
                    snapshot = build_tax_report_snapshot(conn, "2026-11")
                    self.assertEqual(snapshot["tax_profile"]["country_code"], "EG")
                    self.assertEqual(snapshot["summary"]["output_vat"], Decimal("140"))
                    self.assertEqual(snapshot["summary"]["input_vat"], Decimal("56.00"))
                    self.assertEqual(snapshot["sales"][0]["document_no"], "SI-PG-1")
                    self.assertEqual(snapshot["purchases"][0]["document_no"], "PB-PG-1")
                    self.assertTrue(snapshot["ledger_reconciliation"]["available"])
                    self.assertEqual(
                        snapshot["ledger_reconciliation"]["output_difference"],
                        Decimal("0"),
                    )
                    self.assertEqual(
                        snapshot["ledger_reconciliation"]["input_difference"],
                        Decimal("0"),
                    )
                    raise RuntimeError("rollback disposable schema")
            except RuntimeError as exc:
                self.assertEqual(str(exc), "rollback disposable schema")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
