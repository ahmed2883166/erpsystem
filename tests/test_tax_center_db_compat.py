"""Tax center database-neutral metadata helper tests."""
import os
import sqlite3
import unittest
from decimal import Decimal

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


class SpecialTaxQualityCheckTests(unittest.TestCase):
    def test_reports_missing_journal_reference_and_unbalanced_reverse_charge(self):
        from tax_center import _special_tax_quality_issues

        rows = [{
            "document_kind": "purchase", "document_id": 18,
            "mechanism_code": "sa_reverse_charge", "tax_type": None,
            "tax_reference": "", "journal_entry_id": None,
            "output_tax": Decimal("150"), "recoverable_input_tax": Decimal("90"),
            "nonrecoverable_input_tax": Decimal("50"),
        }]
        issues = _special_tax_quality_issues(rows, "SA")
        self.assertEqual(len(issues), 3)
        self.assertTrue(any("قيد يومية" in issue["issue"] for issue in issues))
        self.assertTrue(any("مرجع" in issue["issue"] for issue in issues))
        self.assertTrue(any("غير متوازنة" in issue["issue"] for issue in issues))

    def test_rejects_mechanism_from_other_country(self):
        from tax_center import _special_tax_quality_issues

        rows = [{
            "document_kind": "sale", "document_id": 9,
            "mechanism_code": "eg_table_tax_t3", "tax_type": "T3",
            "tax_reference": "EG-9", "journal_entry_id": 7,
            "output_tax": Decimal("0"), "recoverable_input_tax": Decimal("0"),
            "nonrecoverable_input_tax": Decimal("0"),
        }]
        issues = _special_tax_quality_issues(rows, "SA")
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["severity"], "error")


class PendingSpecialTaxPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""CREATE TABLE company_settings(
            setting_key TEXT PRIMARY KEY, setting_value TEXT
        )""")
        self.conn.execute("INSERT INTO company_settings VALUES('tax_country','EG')")
        self.conn.execute("""CREATE TABLE sales_invoices(
            id INTEGER PRIMARY KEY, status TEXT, invoice_date TEXT,
            total_amount TEXT, tax_amount TEXT
        )""")
        self.conn.execute("""INSERT INTO sales_invoices VALUES(
            4,'posted','2026-11-12','1140.00','140.00'
        )""")
        self.conn.execute("""CREATE TABLE supplier_bills(
            id INTEGER PRIMARY KEY, status TEXT, bill_date TEXT,
            total_amount TEXT, tax_amount TEXT
        )""")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_saves_egypt_table_tax_as_pending_without_changing_invoice(self):
        from tax_center import save_pending_special_tax_mechanism

        result = save_pending_special_tax_mechanism(
            self.conn, "sale", 4, "eg_table_tax_t2",
            taxable_base="1000", tax_reference="ETA-REF-4",
            tax_type="T2", tax_rate="5", created_by=8,
        )
        row = self.conn.execute(
            "SELECT status,tax_period,tax_amount,journal_entry_id,tax_reference "
            "FROM tax_document_mechanisms"
        ).fetchone()
        invoice = self.conn.execute(
            "SELECT total_amount,tax_amount FROM sales_invoices WHERE id=4"
        ).fetchone()
        self.assertEqual(result["status"], "pending")
        self.assertEqual(tuple(row), ("pending","2026-11","50.00",None,"ETA-REF-4"))
        self.assertEqual(tuple(invoice), ("1140.00","140.00"))

    def test_pending_record_can_be_retried_but_posted_record_is_immutable(self):
        from tax_center import save_pending_special_tax_mechanism

        args = dict(taxable_base="1000", tax_reference="ETA-REF-4",
                    tax_type="T2", tax_rate="5")
        save_pending_special_tax_mechanism(
            self.conn, "sale", 4, "eg_table_tax_t2", **args
        )
        save_pending_special_tax_mechanism(
            self.conn, "sale", 4, "eg_table_tax_t2",
            **{**args, "tax_rate": "6"}
        )
        self.conn.execute(
            "UPDATE tax_document_mechanisms SET status='posted',journal_entry_id=91"
        )
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, "قيد مرحّل"):
            save_pending_special_tax_mechanism(
                self.conn, "sale", 4, "eg_table_tax_t2", **args
            )
        row = self.conn.execute(
            "SELECT tax_amount,journal_entry_id,status FROM tax_document_mechanisms"
        ).fetchone()
        self.assertEqual(tuple(row), ("60.00",91,"posted"))

    def test_rejects_wrong_country_and_reverse_charge_on_sales(self):
        from tax_center import save_pending_special_tax_mechanism

        with self.assertRaisesRegex(ValueError, "المصري"):
            save_pending_special_tax_mechanism(
                self.conn, "sale", 4, "sa_reverse_charge",
                taxable_base="1000", tax_reference="REF",
                tax_rate="15", deductible_percent="100",
            )
        self.conn.execute(
            "UPDATE company_settings SET setting_value='SA' WHERE setting_key='tax_country'"
        )
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, "المشتريات"):
            save_pending_special_tax_mechanism(
                self.conn, "sale", 4, "sa_reverse_charge",
                taxable_base="1000", tax_reference="REF",
                tax_rate="15", deductible_percent="100",
            )


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
