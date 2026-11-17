"""PostgreSQL coverage for receivables/payables aging queries in an isolated schema."""
import os
import unittest
import uuid
from decimal import Decimal


@unittest.skipUnless(
    os.environ.get("NEXUS_TEST_POSTGRES_DSN"),
    "NEXUS_TEST_POSTGRES_DSN is not configured",
)
class ReceivablesAgingPostgreSQLTests(unittest.TestCase):
    def test_customer_and_supplier_aging_queries(self):
        import psycopg
        from psycopg.rows import dict_row
        from receivables import (
            _customer_aging_documents, _customer_aging_movements,
            _supplier_aging_documents, _supplier_aging_movements,
        )

        schema = "nexus_aging_" + uuid.uuid4().hex[:12]
        conn = psycopg.connect(
            os.environ["NEXUS_TEST_POSTGRES_DSN"], row_factory=dict_row
        )
        try:
            try:
                with conn.transaction():
                    conn.execute(f'CREATE SCHEMA "{schema}"')
                    conn.execute(f'SET LOCAL search_path TO "{schema}"')
                    conn.execute("CREATE TABLE customers(id BIGINT PRIMARY KEY, code TEXT, name TEXT)")
                    conn.execute("CREATE TABLE suppliers(id BIGINT PRIMARY KEY, code TEXT, name TEXT)")
                    conn.execute("""CREATE TABLE sales_invoices(
                        id BIGINT PRIMARY KEY, customer_id BIGINT, invoice_no TEXT,
                        invoice_date TEXT, due_date TEXT, total_amount TEXT,
                        paid_amount TEXT, credited_amount TEXT, status TEXT)""")
                    conn.execute("""CREATE TABLE supplier_bills(
                        id BIGINT PRIMARY KEY, supplier_id BIGINT, bill_no TEXT,
                        bill_date TEXT, due_date TEXT, total_amount TEXT,
                        paid_amount TEXT, credited_amount TEXT, status TEXT)""")
                    conn.execute("""CREATE TABLE customer_receipts(
                        id BIGINT PRIMARY KEY, invoice_id BIGINT, receipt_date TEXT, amount TEXT)""")
                    conn.execute("""CREATE TABLE customer_receipt_batches(
                        id BIGINT PRIMARY KEY, receipt_date TEXT)""")
                    conn.execute("""CREATE TABLE customer_receipt_allocations(
                        batch_id BIGINT, invoice_id BIGINT, amount TEXT)""")
                    conn.execute("""CREATE TABLE customer_advance_allocations(
                        id BIGINT PRIMARY KEY, invoice_id BIGINT, allocation_date TEXT, amount TEXT)""")
                    conn.execute("""CREATE TABLE sales_credit_notes(
                        id BIGINT PRIMARY KEY, invoice_id BIGINT, total_amount TEXT,
                        credit_date TEXT, status TEXT)""")
                    conn.execute("""CREATE TABLE customer_refunds(
                        id BIGINT PRIMARY KEY, credit_note_id BIGINT,
                        amount TEXT, refund_date TEXT)""")
                    conn.execute("""CREATE TABLE supplier_payments(
                        id BIGINT PRIMARY KEY, payment_date TEXT)""")
                    conn.execute("""CREATE TABLE supplier_payment_allocations(
                        payment_id BIGINT, bill_id BIGINT, amount TEXT)""")
                    conn.execute("""CREATE TABLE supplier_credit_notes(
                        id BIGINT PRIMARY KEY, bill_id BIGINT, total_amount TEXT,
                        status TEXT, credit_date TEXT)""")

                    conn.execute("INSERT INTO customers VALUES(1,'C1','Customer')")
                    conn.execute("INSERT INTO suppliers VALUES(1,'S1','Supplier')")
                    conn.execute("""INSERT INTO sales_invoices VALUES
                        (1,1,'SI-1','2026-11-01','2026-11-10','100','50','10','posted')""")
                    conn.execute("""INSERT INTO supplier_bills VALUES
                        (1,1,'PB-1','2026-11-01','2026-11-10','200','40','15','posted')""")
                    conn.execute("INSERT INTO customer_receipts VALUES(1,1,'2026-11-10','50')")
                    conn.execute("INSERT INTO customer_receipt_batches VALUES(1,'2026-11-12')")
                    conn.execute("INSERT INTO customer_receipt_allocations VALUES(1,1,'5')")
                    conn.execute("INSERT INTO customer_advance_allocations VALUES(1,1,'2026-11-15','3')")
                    conn.execute("INSERT INTO sales_credit_notes VALUES(1,1,'10','2026-11-18','posted')")
                    conn.execute("INSERT INTO customer_refunds VALUES(1,1,'2','2026-11-20')")
                    conn.execute("INSERT INTO supplier_payments VALUES(1,'2026-11-12')")
                    conn.execute("INSERT INTO supplier_payment_allocations VALUES(1,1,'40')")
                    conn.execute("INSERT INTO supplier_credit_notes VALUES(1,1,'15','posted','2026-11-18')")

                    customer_docs = _customer_aging_documents(conn, "2026-11-30")
                    customer_moves = _customer_aging_movements(conn, "2026-11-30")
                    supplier_docs = _supplier_aging_documents(conn, "2026-11-30")
                    supplier_moves = _supplier_aging_movements(conn, "2026-11-30")

                    self.assertEqual(len(customer_docs), 1)
                    self.assertEqual(len(supplier_docs), 1)
                    self.assertTrue(customer_moves[0])
                    self.assertTrue(supplier_moves[0])
                    self.assertEqual(customer_moves[1][1], Decimal("58"))
                    self.assertEqual(customer_moves[3][1], Decimal("10"))
                    self.assertEqual(customer_moves[5][1], Decimal("2"))
                    self.assertEqual(supplier_moves[1][1], Decimal("40"))
                    self.assertEqual(supplier_moves[3][1], Decimal("15"))
                    raise RuntimeError("rollback disposable schema")
            except RuntimeError as exc:
                self.assertEqual(str(exc), "rollback disposable schema")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
