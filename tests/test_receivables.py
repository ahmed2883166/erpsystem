import sqlite3
import tempfile
import unittest
from pathlib import Path
from flask import Flask

import receivables
from support.schema import initialize_party_reporting_schema


class ReceivablesAgingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_path = receivables.DB_PATH
        self.db_path = Path(self.temp.name) / "nexus.db"
        receivables.DB_PATH = self.db_path
        conn = sqlite3.connect(self.db_path)
        initialize_party_reporting_schema(conn)
        conn.executemany("INSERT INTO customers(id,code,name,active) VALUES(?,?,?,?)", [(1,"C1","عميل",1)])
        conn.executemany("INSERT INTO suppliers(id,code,name,active) VALUES(?,?,?,?)", [(1,"S1","مورد",1)])
        conn.execute("""INSERT INTO sales_invoices
            (id,customer_id,invoice_no,invoice_date,due_date,total_amount,paid_amount,credited_amount,status)
            VALUES(1,1,'SI-1','2026-11-01','2026-11-10','100','50','20','posted')""")
        conn.execute("""INSERT INTO sales_credit_notes
            (id,invoice_id,credit_note_no,credit_date,reason,total_amount,status)
            VALUES(1,1,'SCN-1','2026-11-15','مرتجع','20','posted')""")
        conn.execute("""INSERT INTO customer_refunds(id,credit_note_id,refund_no,refund_date,amount)
            VALUES(1,1,'RF-1','2026-11-20','10')""")
        conn.execute("""INSERT INTO customer_refunds(id,credit_note_id,refund_no,refund_date,amount)
            VALUES(2,1,'RF-2','2026-12-01','5')""")
        # Dated subledger records let the report reconstruct the selected historical date.
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS customer_receipts(id INTEGER PRIMARY KEY,invoice_id INTEGER,receipt_date TEXT,amount TEXT);
            CREATE TABLE IF NOT EXISTS customer_receipt_batches(id INTEGER PRIMARY KEY,receipt_date TEXT);
            CREATE TABLE IF NOT EXISTS customer_receipt_allocations(batch_id INTEGER,invoice_id INTEGER,amount TEXT);
            CREATE TABLE IF NOT EXISTS customer_advance_allocations(id INTEGER PRIMARY KEY,invoice_id INTEGER,allocation_date TEXT,amount TEXT);
            CREATE TABLE IF NOT EXISTS supplier_payments(
                id INTEGER PRIMARY KEY,supplier_id INTEGER,payment_date TEXT,
                payment_no TEXT,amount TEXT,reference TEXT);
            CREATE TABLE IF NOT EXISTS supplier_payment_allocations(
                payment_id INTEGER,bill_id INTEGER,amount TEXT);
            CREATE TABLE IF NOT EXISTS supplier_credit_notes(
                id INTEGER PRIMARY KEY,supplier_id INTEGER,bill_id INTEGER,credit_date TEXT,
                credit_no TEXT,reason TEXT,total_amount TEXT,status TEXT);
        """)
        # The party-reporting fixture may pre-create skeletal tables. Upgrade only
        # this isolated test DB to the columns consumed by the real purchase subledger.
        required_columns = {
            "supplier_payments": {
                "payment_no": "TEXT", "amount": "TEXT", "reference": "TEXT",
                "payment_date": "TEXT", "supplier_id": "INTEGER",
            },
            "supplier_payment_allocations": {
                "payment_id": "INTEGER", "bill_id": "INTEGER", "amount": "TEXT",
            },
            "supplier_credit_notes": {
                "supplier_id": "INTEGER", "bill_id": "INTEGER", "credit_date": "TEXT",
                "credit_no": "TEXT", "reason": "TEXT", "total_amount": "TEXT", "status": "TEXT",
            },
        }
        for table, columns in required_columns.items():
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, declaration in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        conn.execute("""INSERT INTO customer_receipts(id,invoice_id,receipt_no,receipt_date,amount,reference)
            VALUES(1,1,'CR-1','2026-11-10','50','')""")
        conn.execute("""INSERT INTO supplier_payments(id,supplier_id,payment_no,payment_date,amount,reference)
            VALUES(1,1,'SP-1','2026-11-12','50','')""")
        conn.execute("INSERT INTO supplier_payment_allocations VALUES(1,1,'50')")
        # A second customer invoice has settlements and credits both before and after the report date.
        conn.execute("""INSERT INTO sales_invoices
            (id,customer_id,invoice_no,invoice_date,due_date,total_amount,paid_amount,credited_amount,status)
            VALUES(2,1,'SI-2','2026-11-02','2026-11-12','200','80','30','posted')""")
        conn.execute("""INSERT INTO sales_credit_notes
            (id,invoice_id,credit_note_no,credit_date,reason,total_amount,status)
            VALUES(2,2,'SCN-2A','2026-11-18','مرتجع قبل التاريخ','10','posted')""")
        conn.execute("""INSERT INTO sales_credit_notes
            (id,invoice_id,credit_note_no,credit_date,reason,total_amount,status)
            VALUES(3,2,'SCN-2B','2026-12-02','مرتجع بعد التاريخ','20','posted')""")
        conn.execute("""INSERT INTO customer_receipts(id,invoice_id,receipt_no,receipt_date,amount,reference)
            VALUES(2,2,'CR-2A','2026-11-25','25','')""")
        conn.execute("""INSERT INTO customer_receipts(id,invoice_id,receipt_no,receipt_date,amount,reference)
            VALUES(3,2,'CR-2B','2026-12-05','30','')""")
        conn.execute("INSERT INTO customer_receipt_batches VALUES(1,'2026-12-06')")
        conn.execute("INSERT INTO customer_receipt_allocations VALUES(1,2,'20')")
        conn.execute("INSERT INTO customer_advance_allocations VALUES(1,2,'2026-11-28','5')")
        conn.execute("""INSERT INTO customer_refunds(id,credit_note_id,refund_no,refund_date,amount)
            VALUES(3,2,'RF-3','2026-11-20','4')""")
        conn.execute("""INSERT INTO customer_refunds(id,credit_note_id,refund_no,refund_date,amount)
            VALUES(4,3,'RF-4','2026-12-04','6')""")
        conn.execute("""INSERT INTO supplier_bills
            (id,supplier_id,bill_no,bill_date,due_date,total_amount,paid_amount,credited_amount,status)
            VALUES(1,1,'PB-1','2026-11-01','2026-11-10','200','50','0','posted')""")
        conn.execute("""INSERT INTO supplier_bills
            (id,supplier_id,bill_no,bill_date,due_date,total_amount,paid_amount,credited_amount,status)
            VALUES(2,1,'PB-2','2026-11-02','2026-11-12','300','80','40','posted')""")
        conn.execute("""INSERT INTO supplier_payments(id,supplier_id,payment_no,payment_date,amount,reference)
            VALUES(2,1,'SP-2A','2026-11-29','30','')""")
        conn.execute("INSERT INTO supplier_payment_allocations VALUES(2,2,'30')")
        conn.execute("""INSERT INTO supplier_payments(id,supplier_id,payment_no,payment_date,amount,reference)
            VALUES(3,1,'SP-2B','2026-12-02','50','')""")
        conn.execute("INSERT INTO supplier_payment_allocations VALUES(3,2,'50')")
        conn.execute("""INSERT INTO supplier_credit_notes(id,credit_no,supplier_id,bill_id,credit_date,reason,total_amount,status)
            VALUES(1,'SCN-2A',1,2,'2026-11-20','مرتجع قبل التاريخ','15','posted')""")
        conn.execute("""INSERT INTO supplier_credit_notes(id,credit_no,supplier_id,bill_id,credit_date,reason,total_amount,status)
            VALUES(2,'SCN-2B',1,2,'2026-12-02','مرتجع بعد التاريخ','25','posted')""")
        conn.commit()
        conn.close()
        # Use a minimal base layout: the real base.html links to app-only endpoints that this test app does not register.
        templates_dir = Path(self.temp.name) / "templates"
        templates_dir.mkdir()
        (templates_dir / "base.html").write_text("{% block content %}{% endblock %}", encoding="utf-8")
        real_report = Path(__file__).resolve().parents[1] / "templates" / "aging_report.html"
        (templates_dir / "aging_report.html").write_text(real_report.read_text(encoding="utf-8"), encoding="utf-8")
        self.app = Flask(__name__, template_folder=str(templates_dir))
        self.app.add_url_rule("/__test_global_search", endpoint="global_search", view_func=lambda: "")
        self.app.secret_key = "test"
        self.app.jinja_env.globals["csrf_token"] = lambda: "test-token"
        self.app.register_blueprint(receivables.aging_bp)
        # The aging page links to the Excel export endpoint; register a stub so url_for can build the link.
        self.app.add_url_rule("/stub-export", endpoint="report_exports.export_report_excel",
                              view_func=lambda: "")
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["role"] = "accountant"

    def tearDown(self):
        receivables.DB_PATH = self.original_path
        self.temp.cleanup()

    def test_customer_aging_uses_only_refunds_as_of_report_date(self):
        response = self.client.get("/reports/aging/?kind=customers&as_of=2026-11-30")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("40", body)
        self.assertNotIn("45.00", body)

    def test_customer_aging_uses_receipts_advances_credits_and_refunds_as_of_date(self):
        response = self.client.get("/reports/aging/?kind=customers&as_of=2026-11-30")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        # Invoice 2: 200 - 25 direct receipt - 5 advance - 10 credit + 4 refund = 164.
        self.assertIn("164.00", body)
        # Later receipt, batch allocation, credit note and refund must not affect 30 November.
        self.assertNotIn("139.00", body)

    def test_supplier_aging_uses_payments_and_credit_notes_as_of_date(self):
        response = self.client.get("/reports/aging/?kind=suppliers&as_of=2026-11-30")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        # Bill 2: 300 - 30 payment - 15 credit note = 255.
        self.assertIn("255.00", body)
        # The December payment and credit note are excluded.
        self.assertNotIn("205.00", body)

    def test_aging_excel_exports_match_historical_screen_balances(self):
        from flask import Flask
        from io import BytesIO
        from openpyxl import load_workbook
        from report_exports import register_report_exports

        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE company_settings(setting_key TEXT PRIMARY KEY,setting_value TEXT)")
        conn.executemany("INSERT INTO company_settings VALUES(?,?)", [
            ("company_name", "شركة الاختبار"), ("base_currency", "EGP")
        ])
        conn.commit()
        conn.close()

        export_app = Flask(__name__)
        export_app.secret_key = "aging-export-test-secret"
        register_report_exports(export_app, self.db_path, {"accountant": {"reports.view"}})
        client = export_app.test_client()
        with client.session_transaction() as user_session:
            user_session["user_id"] = 1
            user_session["role"] = "accountant"
            user_session["permissions"] = ["reports.view"]

        # Older databases can have supplier_bills before the purchases migration
        # adds credited_amount. Both XLSX and PDF exports must remain available.
        conn = sqlite3.connect(self.db_path)
        conn.execute("ALTER TABLE supplier_bills DROP COLUMN credited_amount")
        conn.commit()
        conn.close()

        for kind, expected in (("customers", 164), ("suppliers", 255)):
            response = client.get(
                f"/reports/export/aging/excel?kind={kind}&as_of=2026-11-30"
            )
            self.assertEqual(response.status_code, 200)
            workbook = load_workbook(BytesIO(response.data), data_only=True)
            values = [cell.value for row in workbook.active.iter_rows() for cell in row]
            self.assertIn(expected, values)

        # Simulate a legacy/partially initialized allocation table. The PDF path
        # must fall back to bill-level paid_amount instead of failing on bill_id.
        conn = sqlite3.connect(self.db_path)
        conn.execute("ALTER TABLE supplier_payment_allocations RENAME TO supplier_payment_allocations_full")
        conn.execute("CREATE TABLE supplier_payment_allocations(payment_id INTEGER,amount TEXT)")
        conn.execute("DROP TABLE supplier_payment_allocations_full")
        conn.commit()
        conn.close()

        pdf_response = client.get(
            "/reports/export/aging/pdf?kind=suppliers&as_of=2026-11-30"
        )
        self.assertEqual(pdf_response.status_code, 200)
        self.assertEqual(pdf_response.mimetype, "application/pdf")
        self.assertTrue(pdf_response.data.startswith(b"%PDF"))


    def test_supplier_aging_supports_legacy_supplier_table_without_code_or_name(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("ALTER TABLE suppliers RENAME TO suppliers_full")
        conn.execute("CREATE TABLE suppliers(id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO suppliers(id) VALUES(1)")
        conn.commit()
        conn.row_factory = sqlite3.Row
        documents = receivables._supplier_aging_documents(conn, "2026-11-30")
        conn.close()

        self.assertEqual(len(documents), 2)
        self.assertEqual(documents[0]["party_code"], "1")
        self.assertEqual(documents[0]["party_name"], "1")


    def test_customer_aging_handles_legacy_invoice_table_without_credit_column(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("ALTER TABLE sales_invoices DROP COLUMN credited_amount")
        conn.commit()
        conn.close()

        response = self.client.get("/reports/aging/?kind=customers&as_of=2026-11-30")
        self.assertEqual(response.status_code, 200)
        # Credit notes are still reconstructed from their dated subledger rows.
        self.assertIn("164.00", response.get_data(as_text=True))

    def test_supplier_aging_handles_bill_table_without_purchase_credit_column(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("ALTER TABLE supplier_bills DROP COLUMN credited_amount")
        conn.commit()
        conn.close()

        response = self.client.get("/reports/aging/?kind=suppliers&as_of=2026-11-30")
        self.assertEqual(response.status_code, 200)
        self.assertIn("255.00", response.get_data(as_text=True))

    def test_supplier_aging_is_empty_before_supplier_bills_table_is_initialized(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("DROP TABLE supplier_bills")
        conn.commit()
        conn.close()

        receivables.initialize_aging_schema()
        response = self.client.get("/reports/aging/?kind=suppliers&as_of=2026-11-30")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("PB-1", response.get_data(as_text=True))

    def test_supplier_aging_does_not_require_customer_invoice_fields(self):
        response = self.client.get("/reports/aging/?kind=suppliers&as_of=2026-11-30")
        self.assertEqual(response.status_code, 200)
        self.assertIn("150", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
