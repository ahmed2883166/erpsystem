"""Tax center database-neutral metadata helper tests."""
import os
import sqlite3
import unittest

from tax_center import _columns, _table_exists

class TaxMechanismSchemaSQLiteTests(unittest.TestCase):
    def test_special_tax_storage_migration_is_additive_and_idempotent(self):
        from tax_center import initialize_tax_schema

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("""CREATE TABLE sales_invoices(
                id INTEGER PRIMARY KEY, invoice_no TEXT NOT NULL,
                status TEXT NOT NULL, tax_amount TEXT NOT NULL DEFAULT '0'
            )""")
            conn.execute("""INSERT INTO sales_invoices(id,invoice_no,status,tax_amount)
                VALUES(7,'OLD-007','posted','14.00')""")
            conn.commit()

            initialize_tax_schema(conn)
            initialize_tax_schema(conn)

            columns = _columns(conn, "tax_document_mechanisms")
            self.assertTrue({
                "country_code", "document_kind", "document_id", "tax_period", "mechanism_code",
                "tax_type", "taxable_base", "tax_rate", "quantity",
                "amount_per_unit", "deductible_percent", "output_tax",
                "recoverable_input_tax", "nonrecoverable_input_tax",
                "tax_amount", "journal_entry_id", "status",
            }.issubset(columns))
            legacy = conn.execute(
                "SELECT invoice_no,status,tax_amount FROM sales_invoices WHERE id=7"
            ).fetchone()
            self.assertEqual(tuple(legacy), ("OLD-007", "posted", "14.00"))
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM tax_document_mechanisms").fetchone()[0],
                0,
            )
        finally:
            conn.close()


class SpecialTaxPeriodReaderTests(unittest.TestCase):
    def test_reads_only_posted_rows_for_exact_country_and_period(self):
        from tax_center import _special_tax_mechanisms_for_period

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("""CREATE TABLE tax_document_mechanisms(
                country_code TEXT, document_kind TEXT, document_id INTEGER,
                tax_period TEXT, mechanism_code TEXT, tax_type TEXT,
                taxable_base TEXT, tax_rate TEXT, quantity TEXT, amount_per_unit TEXT,
                deductible_percent TEXT, output_tax TEXT,
                recoverable_input_tax TEXT, nonrecoverable_input_tax TEXT,
                tax_amount TEXT, tax_reference TEXT, journal_entry_id INTEGER,
                status TEXT
            )""")
            conn.executemany("""INSERT INTO tax_document_mechanisms VALUES(
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )""", [
                ("EG","sale",1,"2026-11","eg_table_tax_t2","T2","100","10",None,None,None,"0","0","0","10","EG-T2",11,"posted"),
                ("EG","sale",2,"2026-11","eg_table_tax_t3","T3","200",None,"3","4",None,"0","0","0","12","EG-T3",12,"posted"),
                ("EG","sale",3,"2026-11","eg_table_tax_t2","T2","100","10",None,None,None,"0","0","0","10","PENDING",None,"pending"),
                ("EG","sale",4,"2026-10","eg_table_tax_t2","T2","100","10",None,None,None,"0","0","0","10","OTHER-PERIOD",13,"posted"),
                ("SA","purchase",5,"2026-11","sa_reverse_charge",None,"1000","15",None,None,"60","150","90","60","0","SA-RCM",14,"posted"),
            ])
            rows, totals = _special_tax_mechanisms_for_period(conn, "EG", "2026-11")
            self.assertEqual(len(rows), 2)
            self.assertEqual(totals["egypt_table_tax"], Decimal("22"))
            self.assertEqual(totals["saudi_reverse_charge_output"], Decimal("0"))
            self.assertEqual(rows[0]["tax_amount"], Decimal("10"))
        finally:
            conn.close()

    def test_missing_special_tax_table_returns_empty_totals(self):
        from tax_center import _special_tax_mechanisms_for_period
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            rows, totals = _special_tax_mechanisms_for_period(conn, "SA", "2026-Q4")
            self.assertEqual(rows, [])
            self.assertEqual(totals["saudi_reverse_charge_output"], Decimal("0"))
        finally:
            conn.close()


class TaxMetadataSQLiteTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE tax_probe (id INTEGER PRIMARY KEY, tax_reference TEXT)")

    def tearDown(self):
        self.conn.close()

    def test_table_exists(self):
        self.assertTrue(_table_exists(self.conn, "tax_probe"))
        self.assertFalse(_table_exists(self.conn, "missing_probe"))

    def test_columns(self):
        self.assertEqual(_columns(self.conn, "tax_probe"), {"id", "tax_reference"})
        self.assertEqual(_columns(self.conn, "missing_probe"), set())

@unittest.skipUnless(os.environ.get("NEXUS_TEST_POSTGRES_DSN"), "PostgreSQL integration DSN not configured")
class TaxMetadataPostgreSQLTests(unittest.TestCase):
    def test_metadata_helpers_in_rolled_back_schema(self):
        import uuid
        import psycopg
        from psycopg.rows import dict_row
        schema = "nexus_tax_meta_" + uuid.uuid4().hex[:12]
        conn = psycopg.connect(os.environ["NEXUS_TEST_POSTGRES_DSN"], row_factory=dict_row)
        try:
            try:
                with conn.transaction():
                    conn.execute(f'CREATE SCHEMA "{schema}"')
                    conn.execute(f'SET LOCAL search_path TO "{schema}"')
                    conn.execute("CREATE TABLE tax_probe (id BIGINT PRIMARY KEY, tax_reference TEXT)")
                    self.assertTrue(_table_exists(conn, "tax_probe"))
                    self.assertFalse(_table_exists(conn, "missing_probe"))
                    self.assertEqual(_columns(conn, "tax_probe"), {"id", "tax_reference"})
                    self.assertEqual(_columns(conn, "missing_probe"), set())
                    raise RuntimeError("rollback disposable schema")
            except RuntimeError as exc:
                self.assertEqual(str(exc), "rollback disposable schema")
        finally:
            conn.close()

if __name__ == "__main__":
    unittest.main()
