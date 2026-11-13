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
        receivables.DB_PATH = Path(self.temp.name) / "nexus.db"
        conn = sqlite3.connect(receivables.DB_PATH)
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
            CREATE TABLE customer_receipts(id INTEGER PRIMARY KEY,invoice_id INTEGER,receipt_date TEXT,amount TEXT);
            CREATE TABLE customer_receipt_batches(id INTEGER PRIMARY KEY,receipt_date TEXT);
            CREATE TABLE customer_receipt_allocations(batch_id INTEGER,invoice_id INTEGER,amount TEXT);
            CREATE TABLE customer_advance_allocations(id INTEGER PRIMARY KEY,invoice_id INTEGER,allocation_date TEXT,amount TEXT);
            CREATE TABLE supplier_payments(id INTEGER PRIMARY KEY,supplier_id INTEGER,payment_date TEXT);
            CREATE TABLE supplier_payment_allocations(payment_id INTEGER,bill_id INTEGER,amount TEXT);
            CREATE TABLE supplier_credit_notes(id INTEGER PRIMARY KEY,supplier_id INTEGER,bill_id INTEGER,credit_date TEXT,total_amount TEXT,status TEXT);
        """)
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

    def test_supplier_aging_does_not_require_customer_invoice_fields(self):
        response = self.client.get("/reports/aging/?kind=suppliers&as_of=2026-11-30")
        self.assertEqual(response.status_code, 200)
        self.assertIn("150", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
