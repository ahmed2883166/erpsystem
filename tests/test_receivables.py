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
        conn.execute("""INSERT INTO supplier_bills
            (id,supplier_id,bill_no,bill_date,due_date,total_amount,paid_amount,credited_amount,status)
            VALUES(1,1,'PB-1','2026-11-01','2026-11-10','200','50','0','posted')""")
        conn.commit()
        conn.close()
        # Use a minimal base layout: the real base.html links to app-only endpoints that this test app does not register.
        templates_dir = Path(self.temp.name) / "templates"
        templates_dir.mkdir()
        (templates_dir / "base.html").write_text("{% block content %}{% endblock %}", encoding="utf-8")
        real_report = Path(__file__).resolve().parents[1] / "templates" / "aging_report.html"
        (templates_dir / "aging_report.html").write_text(real_report.read_text(encoding="utf-8"), encoding="utf-8")
        self.app = Flask(__name__, template_folder=str(templates_dir))
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

    def test_supplier_aging_does_not_require_customer_invoice_fields(self):
        response = self.client.get("/reports/aging/?kind=suppliers&as_of=2026-11-30")
        self.assertEqual(response.status_code, 200)
        self.assertIn("150", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
