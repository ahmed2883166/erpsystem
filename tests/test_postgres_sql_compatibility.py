"""PostgreSQL execution tests for known cross-dialect query shapes."""
import os
import unittest

from postgres_database import connect_postgres

DSN = os.environ.get("NEXUS_TEST_POSTGRES_DSN", "").strip()


@unittest.skipUnless(DSN, "NEXUS_TEST_POSTGRES_DSN is not configured")
class PostgreSQLSQLCompatibilityTests(unittest.TestCase):
    def test_sales_party_payroll_and_activity_monitor_queries(self):
        connection = connect_postgres(DSN, schema="public")
        try:
            statements = (
                "CREATE TEMP TABLE customer_receipt_batches(id BIGINT PRIMARY KEY, customer_id BIGINT)",
                "CREATE TEMP TABLE customers(id BIGINT PRIMARY KEY, name TEXT)",
                "CREATE TEMP TABLE customer_receipt_allocations(id BIGINT PRIMARY KEY, batch_id BIGINT, invoice_id BIGINT, amount NUMERIC)",
                "CREATE TEMP TABLE sales_invoices(id BIGINT PRIMARY KEY, invoice_no TEXT)",
                "CREATE TEMP TABLE payroll_runs(id BIGINT PRIMARY KEY, period TEXT, status TEXT, journal_entry_id BIGINT)",
                "CREATE TEMP TABLE payroll_lines(id BIGINT PRIMARY KEY, employee_id BIGINT, run_id BIGINT, net_pay NUMERIC)",
                "CREATE TEMP TABLE employees(id BIGINT PRIMARY KEY, employee_code TEXT, full_name TEXT)",
                "CREATE TEMP TABLE payroll_payment_allocations(id BIGINT PRIMARY KEY, payroll_line_id BIGINT, payment_id BIGINT, amount NUMERIC)",
                "CREATE TEMP TABLE payroll_payments(id BIGINT PRIMARY KEY, run_id BIGINT)",
                "CREATE TEMP TABLE employee_final_payrolls(id BIGINT PRIMARY KEY, period TEXT, employee_id BIGINT, final_payroll_no TEXT, status TEXT)",
                "CREATE TEMP TABLE audit_log(id BIGINT PRIMARY KEY, action TEXT, outcome TEXT, entity_type TEXT, before_values TEXT, after_values TEXT, created_at TEXT)",
                "CREATE TEMP TABLE customer_advance_allocations(id BIGINT PRIMARY KEY, allocation_date TEXT, invoice_id BIGINT, amount NUMERIC)",
                "CREATE TEMP TABLE customer_advance_reversals(id BIGINT PRIMARY KEY, reversal_date TEXT, source_id BIGINT, source_type TEXT)",
            )
            for statement in statements:
                connection.execute(statement)
            connection.execute("INSERT INTO customers VALUES(1,'عميل')")
            connection.execute("INSERT INTO customer_receipt_batches VALUES(1,1)")
            connection.execute("INSERT INTO sales_invoices VALUES(1,'INV-1')")
            connection.execute("INSERT INTO customer_receipt_allocations VALUES(1,1,1,25)")
            connection.execute("INSERT INTO payroll_runs VALUES(1,'2026-11','posted',NULL)")
            connection.execute("INSERT INTO employees VALUES(1,'EMP-1','موظف')")
            connection.execute("INSERT INTO payroll_lines VALUES(1,1,1,100)")
            connection.execute("INSERT INTO payroll_payments VALUES(1,1)")
            connection.execute("INSERT INTO payroll_payment_allocations VALUES(1,1,1,150)")
            connection.execute("INSERT INTO employee_final_payrolls VALUES(1,'2026-11',1,'FP-1','paid')")
            connection.execute("INSERT INTO audit_log VALUES(1,'login','success','auth',NULL,NULL,'2026-11-19 12:00:00')")
            connection.execute("INSERT INTO customer_advance_allocations VALUES(1,'2026-11-01',1,20)")
            connection.execute("INSERT INTO customer_advance_reversals VALUES(1,'2026-11-02',1,'allocation')")

            sales_row = connection.execute("""
                SELECT b.id,cu.name,COUNT(a.id) AS invoice_count,
                       STRING_AGG(i.invoice_no || ' (' || CAST(a.amount AS TEXT) || ')', ', ') AS invoice_numbers
                FROM customer_receipt_batches b
                JOIN customers cu ON cu.id=b.customer_id
                LEFT JOIN customer_receipt_allocations a ON a.batch_id=b.id
                LEFT JOIN sales_invoices i ON i.id=a.invoice_id
                GROUP BY b.id,cu.name
            """).fetchone()
            self.assertEqual(sales_row["invoice_count"], 1)

            party_row = connection.execute("""
                SELECT 'CADVA-' || CAST(a.id AS TEXT) AS reference,
                       'CADVA-REV-' || CAST(x.id AS TEXT) AS reversal_reference
                FROM customer_advance_allocations a
                JOIN sales_invoices i ON i.id=a.invoice_id
                JOIN customer_advance_reversals x ON x.source_id=a.id
            """).fetchone()
            self.assertEqual(party_row["reference"], "CADVA-1")
            self.assertEqual(party_row["reversal_reference"], "CADVA-REV-1")

            payroll_row = connection.execute("""
                SELECT e.employee_code,e.full_name,COUNT(*) AS occurrences
                FROM payroll_lines l JOIN employees e ON e.id=l.employee_id
                WHERE l.run_id=%s
                GROUP BY l.employee_id,e.employee_code,e.full_name
            """, (1,)).fetchone()
            self.assertEqual(payroll_row["employee_code"], "EMP-1")

            overallocated = connection.execute("""
                SELECT pl.id,e.employee_code,e.full_name,pl.net_pay,
                       SUM(CAST(a.amount AS NUMERIC)) AS allocated
                FROM payroll_lines pl
                JOIN employees e ON e.id=pl.employee_id
                JOIN payroll_payment_allocations a ON a.payroll_line_id=pl.id
                JOIN payroll_payments p ON p.id=a.payment_id
                WHERE pl.run_id=%s
                GROUP BY pl.id,e.employee_code,e.full_name,pl.net_pay
                HAVING SUM(CAST(a.amount AS NUMERIC))>CAST(pl.net_pay AS NUMERIC)
            """, (1,)).fetchone()
            self.assertEqual(overallocated["allocated"], 150)

            duplicate_runs = connection.execute("""
                SELECT r.period,e.employee_code,e.full_name,COUNT(*) AS occurrences,
                       STRING_AGG(CAST(r.id AS TEXT), ', ') AS run_ids
                FROM payroll_runs r
                JOIN payroll_lines l ON l.run_id=r.id
                JOIN employees e ON e.id=l.employee_id
                WHERE r.status='posted'
                GROUP BY r.period,l.employee_id,e.employee_code,e.full_name
            """).fetchone()
            self.assertEqual(duplicate_runs["employee_code"], "EMP-1")

            final_row = connection.execute("""
                SELECT period,e.employee_code,e.full_name,COUNT(*) AS occurrences,
                       STRING_AGG(final_payroll_no, ', ') AS final_numbers
                FROM employee_final_payrolls f JOIN employees e ON e.id=f.employee_id
                GROUP BY period,employee_id,e.employee_code,e.full_name
            """).fetchone()
            self.assertEqual(final_row["final_numbers"], "FP-1")

            activity_row = connection.execute("""
                SELECT COUNT(*) AS count FROM audit_log WHERE substr(created_at,1,10)>=%s
            """, ("2026-11-13",)).fetchone()
            self.assertEqual(activity_row["count"], 1)
        finally:
            connection.rollback()
            connection.close()


if __name__ == "__main__":
    unittest.main()
