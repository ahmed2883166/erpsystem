"""Exercise party-report query execution on PostgreSQL without touching app data."""
import os
import unittest

from party_reports import (
    _all_party_events, _events, _gl_control_balance,
    _gl_party_control_balances,
)
from management_dashboard import _copied_opening_entry_ids


@unittest.skipUnless(os.environ.get("NEXUS_TEST_POSTGRES_DSN"),
                     "NEXUS_TEST_POSTGRES_DSN is not configured")
class PartyReportsPostgreSQLQueryTests(unittest.TestCase):
    def test_copied_opening_entry_exclusion_executes_on_postgresql(self):
        import psycopg
        from psycopg.rows import dict_row

        connection = psycopg.connect(
            os.environ["NEXUS_TEST_POSTGRES_DSN"], row_factory=dict_row
        )
        schema = "nexus_party_opening_test"
        try:
            connection.execute(f'CREATE SCHEMA "{schema}"')
            connection.execute(f'SET search_path TO "{schema}"')
            statements = (
                "CREATE TABLE journal_entries (id BIGINT PRIMARY KEY, entry_date TEXT, status TEXT)",
                "CREATE TABLE accounts (id BIGINT PRIMARY KEY, code TEXT, is_group BIGINT, active BIGINT)",
                "CREATE TABLE journal_lines (id BIGINT PRIMARY KEY, entry_id BIGINT, account_id BIGINT, debit NUMERIC(24,6), credit NUMERIC(24,6))",
                "CREATE TABLE financial_years (id BIGINT PRIMARY KEY, start_date TEXT)",
                """CREATE TABLE financial_year_journal_history (
                    id BIGINT PRIMARY KEY, financial_year_id BIGINT,
                    entry_kind TEXT, action TEXT, entry_id BIGINT,
                    related_entry_id BIGINT
                )""",
            )
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO journal_entries VALUES (1, '2026-12-31', 'posted'), (2, '2027-01-01', 'posted'), (3, '2027-01-02', 'posted')"
            )
            connection.execute("INSERT INTO financial_years VALUES (1, '2027-01-01')")
            connection.execute("INSERT INTO accounts VALUES (1, '1100', 0, 1)")
            connection.execute(
                "INSERT INTO journal_lines VALUES (1, 1, 1, 100.00, 0), (2, 2, 1, 100.00, 0), (3, 3, 1, 0, 100.00)"
            )
            connection.execute(
                """INSERT INTO financial_year_journal_history
                   VALUES (1, 1, 'opening', 'created', 2, NULL),
                          (2, 1, 'opening', 'reversed', 3, 2)"""
            )
            self.assertEqual(
                _copied_opening_entry_ids(connection, "2027-02-01"), {2, 3}
            )
            balance, matched_codes = _gl_control_balance(
                connection, ["1100"], "2027-02-01", "debit"
            )
            self.assertEqual(balance, 100)
            self.assertEqual(matched_codes, ["1100"])
        finally:
            connection.rollback()
            connection.close()

    def test_customer_and_supplier_statement_queries_execute_on_postgresql(self):
        import psycopg
        from psycopg.rows import dict_row

        connection = psycopg.connect(
            os.environ["NEXUS_TEST_POSTGRES_DSN"], row_factory=dict_row
        )
        schema = "nexus_party_query_test"
        try:
            connection.execute(f'CREATE SCHEMA "{schema}"')
            connection.execute(f'SET search_path TO "{schema}"')
            ddl_statements = (
                """CREATE TABLE sales_invoices (
                    id BIGINT PRIMARY KEY, customer_id BIGINT, invoice_date TEXT,
                    invoice_no TEXT, status TEXT, total_amount NUMERIC(18,2)
                )""",
                """CREATE TABLE customer_receipts (
                    id BIGINT PRIMARY KEY, invoice_id BIGINT, receipt_date TEXT,
                    receipt_no TEXT, reference TEXT, amount NUMERIC(18,2)
                )""",
                """CREATE TABLE sales_credit_notes (
                    id BIGINT PRIMARY KEY, invoice_id BIGINT, credit_date TEXT,
                    credit_note_no TEXT, reason TEXT, total_amount NUMERIC(18,2),
                    status TEXT
                )""",
                """CREATE TABLE supplier_bills (
                    id BIGINT PRIMARY KEY, supplier_id BIGINT, bill_date TEXT,
                    bill_no TEXT, status TEXT, total_amount NUMERIC(18,2)
                )""",
                """CREATE TABLE supplier_credit_notes (
                    id BIGINT PRIMARY KEY, supplier_id BIGINT, credit_date TEXT,
                    credit_no TEXT, reason TEXT, total_amount NUMERIC(18,2),
                    status TEXT
                )""",
                """CREATE TABLE supplier_payments (
                    id BIGINT PRIMARY KEY, supplier_id BIGINT, payment_date TEXT,
                    payment_no TEXT, reference TEXT, amount NUMERIC(18,2)
                )""",
            )
            for statement in ddl_statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO sales_invoices VALUES (1, 10, '2026-11-01', 'SI-1', 'posted', 125.00)"
            )
            connection.execute(
                "INSERT INTO customer_receipts VALUES (1, 1, '2026-11-02', 'CR-1', 'BANK', 25.00)"
            )
            connection.execute(
                "INSERT INTO sales_credit_notes VALUES (1, 1, '2026-11-03', 'SCN-1', 'return', 5.00, 'posted')"
            )
            connection.execute(
                "INSERT INTO supplier_bills VALUES (1, 20, '2026-11-01', 'PB-1', 'posted', 200.00)"
            )
            connection.execute(
                "INSERT INTO supplier_credit_notes VALUES (1, 20, '2026-11-02', 'PCN-1', 'return', 20.00, 'posted')"
            )
            connection.execute(
                "INSERT INTO supplier_payments VALUES (1, 20, '2026-11-03', 'SP-1', 'BANK', 30.00)"
            )

            customer_events = _events(connection, "customer", 10)
            self.assertEqual(len(customer_events), 3)
            self.assertEqual(sum((e["debit"] for e in customer_events)), 125)
            self.assertEqual(sum((e["credit"] for e in customer_events)), 30)

            supplier_events = _events(connection, "supplier", 20)
            self.assertEqual(len(supplier_events), 3)
            self.assertEqual(sum((e["debit"] for e in supplier_events)), 50)
            self.assertEqual(sum((e["credit"] for e in supplier_events)), 200)

            customer_all = _all_party_events(connection, "customer")
            self.assertEqual(len(customer_all), 3)
            self.assertEqual(sum((e["debit"] for e in customer_all)), 125)
            self.assertEqual(sum((e["credit"] for e in customer_all)), 30)

            supplier_all = _all_party_events(connection, "supplier")
            self.assertEqual(len(supplier_all), 3)
            self.assertEqual(sum((e["debit"] for e in supplier_all)), 50)
            self.assertEqual(sum((e["credit"] for e in supplier_all)), 200)
        finally:
            connection.rollback()
            connection.close()


    def test_party_level_gl_balances_and_unassigned_dimension_on_postgresql(self):
        import psycopg
        from psycopg.rows import dict_row

        connection = psycopg.connect(
            os.environ["NEXUS_TEST_POSTGRES_DSN"], row_factory=dict_row
        )
        schema = "nexus_party_dimension_test"
        try:
            connection.execute(f'CREATE SCHEMA "{schema}"')
            connection.execute(f'SET search_path TO "{schema}"')
            connection.execute("""
                CREATE TABLE accounts (
                    id BIGINT PRIMARY KEY, code TEXT, is_group BIGINT, active BIGINT
                )
            """)
            connection.execute("""
                CREATE TABLE journal_entries (
                    id BIGINT PRIMARY KEY, entry_date TEXT, status TEXT
                )
            """)
            connection.execute("""
                CREATE TABLE journal_lines (
                    id BIGINT PRIMARY KEY, entry_id BIGINT, account_id BIGINT,
                    customer_id BIGINT, supplier_id BIGINT,
                    debit NUMERIC(24,6), credit NUMERIC(24,6)
                )
            """)
            connection.execute(
                "INSERT INTO accounts VALUES (1, '1130', 0, 1), (2, '2110', 0, 1)"
            )
            connection.execute(
                "INSERT INTO journal_entries VALUES (1, '2026-11-01', 'posted')"
            )
            connection.execute("""
                INSERT INTO journal_lines
                    (id, entry_id, account_id, customer_id, supplier_id, debit, credit)
                VALUES
                    (1, 1, 1, 10, NULL, 150, 0),
                    (2, 1, 1, 10, NULL, 0, 25),
                    (3, 1, 1, 11, NULL, 80, 0),
                    (4, 1, 2, NULL, 20, 0, 200),
                    (5, 1, 2, NULL, 20, 30, 0)
            """)
            customer_balances, customer_available = _gl_party_control_balances(
                connection, ["1130"], "2026-12-31", "debit", "customer_id"
            )
            self.assertTrue(customer_available)
            self.assertEqual(customer_balances, {10: 125, 11: 80})

            supplier_balances, supplier_available = _gl_party_control_balances(
                connection, ["2110"], "2026-12-31", "credit", "supplier_id"
            )
            self.assertTrue(supplier_available)
            self.assertEqual(supplier_balances, {20: 170})

            connection.execute("""
                INSERT INTO journal_lines
                    (id, entry_id, account_id, customer_id, supplier_id, debit, credit)
                VALUES (6, 1, 1, NULL, NULL, 10, 0)
            """)
            unassigned_balances, unassigned_available = _gl_party_control_balances(
                connection, ["1130"], "2026-12-31", "debit", "customer_id"
            )
            self.assertFalse(unassigned_available)
            self.assertEqual(unassigned_balances, {})
        finally:
            connection.rollback()
            connection.close()



if __name__ == "__main__":
    unittest.main()
