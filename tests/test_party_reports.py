import sqlite3
import tempfile
import unittest
from pathlib import Path

from flask import Flask
import party_reports
from support.schema import initialize_party_reporting_schema


class PartyStatementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "nexus.db"
        self.original_path = party_reports.DB_PATH
        party_reports.DB_PATH = self.db_path
        conn = sqlite3.connect(self.db_path)
        initialize_party_reporting_schema(conn)
        conn.executemany("INSERT INTO customers(id,code,name,active) VALUES(?,?,?,?)", [(1,"C001","Customer",1)])
        conn.executemany("INSERT INTO suppliers(id,code,name,active) VALUES(?,?,?,?)", [(1,"S001","Supplier",1)])
        conn.executemany("""INSERT INTO sales_invoices
            (id,customer_id,invoice_no,invoice_date,due_date,total_amount,paid_amount,credited_amount,status)
            VALUES(?,?,?,?,?,?,?,?,?)""", [
            (1,1,"SI-OLD","2026-10-10","2026-10-10","1000","0","0","posted"),
            (2,1,"SI-NOW","2026-11-10","2026-11-10","500","0","0","posted")])
        conn.executemany("""INSERT INTO customer_receipts
            (id,invoice_id,receipt_no,receipt_date,amount,reference) VALUES(?,?,?,?,?,?)""", [
            (1,1,"CR-OLD","2026-10-20","200",""),(2,2,"CR-NOW","2026-11-20","100","")])
        conn.executemany("""INSERT INTO supplier_bills
            (id,supplier_id,bill_no,bill_date,due_date,total_amount,paid_amount,credited_amount,status)
            VALUES(?,?,?,?,?,?,?,?,?)""", [
            (1,1,"PB-OLD","2026-10-10","2026-10-10","2000","0","0","posted"),
            (2,1,"PB-NOW","2026-11-10","2026-11-10","400","0","0","posted")])
        conn.executemany("""INSERT INTO supplier_payments
            (id,supplier_id,payment_no,payment_date,amount,reference) VALUES(?,?,?,?,?,?)""", [
            (1,1,"SP-OLD","2026-10-20","500",""),(2,1,"SP-NOW","2026-11-20","200","")])
        conn.commit()
        conn.close()
        template_dir = Path(__file__).resolve().parents[1] / "templates"
        self.app = Flask(__name__, template_folder=str(template_dir))
        self.app.add_url_rule("/__test_global_search", endpoint="global_search", view_func=lambda: "")
        self.app.add_url_rule(
            "/reports/export/<report>/excel", endpoint="report_exports.export_report_excel",
            view_func=lambda report: ""
        )
        self.app.jinja_env.globals["csrf_token"] = lambda: "test-token"
        self.app.jinja_env.filters["money"] = lambda value: f"{float(value or 0):,.2f}"
        self.app.context_processor(lambda: {"license_info": {"status": "demo"}, "permissions": set()})
        self.app.add_url_rule("/change-password", endpoint="change_password", view_func=lambda: "")
        self.app.add_url_rule("/logout", endpoint="logout", view_func=lambda: "")
        self.app.add_url_rule("/dashboard", endpoint="dashboard", view_func=lambda: "")
        self.app.secret_key = "test"
        self.app.register_blueprint(party_reports.party_bp)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["role"] = "accountant"

    def tearDown(self):
        party_reports.DB_PATH = self.original_path
        self.temp.cleanup()

    def test_customer_statement_opening_and_running_balance(self):
        response = self.client.get("/reports/party-statement?kind=customer&party_id=1&date_from=2026-11-01&date_to=2026-11-30")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("كشف حساب عميل", body)
        self.assertIn("1,200.00", body)

    def test_customer_statement_includes_credit_notes_and_cash_refunds(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO sales_credit_notes(id,invoice_id,credit_note_no,credit_date,reason,total_amount,status) VALUES(1,2,'SCN-1','2026-11-21','مرتجع','20.00','posted')"
        )
        conn.execute(
            "INSERT INTO customer_refunds(id,credit_note_id,refund_no,refund_date,amount) VALUES(1,1,'SREF-1','2026-11-22','5.00')"
        )
        conn.commit()
        conn.close()
        response = self.client.get(
            "/reports/party-statement?kind=customer&party_id=1&date_from=2026-11-01&date_to=2026-11-30"
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("كشف حساب عميل", body)
        self.assertIn("SCN-1", body)

    def test_customer_statement_includes_batch_receipts_without_double_counting(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""CREATE TABLE customer_receipt_batches(
            id INTEGER PRIMARY KEY, customer_id INTEGER, receipt_no TEXT,
            receipt_date TEXT, total_amount TEXT, reference TEXT
        )""")
        conn.execute("""CREATE TABLE customer_receipt_allocations(
            id INTEGER PRIMARY KEY, batch_id INTEGER, invoice_id INTEGER, amount TEXT
        )""")
        conn.execute(
            "INSERT INTO customer_receipt_batches VALUES(1,1,'RCPTB-1','2026-11-25','150.00','BANK-1')"
        )
        conn.execute(
            "INSERT INTO customer_receipt_allocations VALUES(1,1,2,'150.00')"
        )
        conn.commit()
        conn.close()

        response = self.client.get(
            "/reports/party-statement?kind=customer&party_id=1&date_from=2026-11-01&date_to=2026-11-30"
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("RCPTB-1", body)
        self.assertIn("1,050.00", body)  # 500 invoice - 100 receipt - 150 batch receipt + 200 old invoice net in opening.

    def test_supplier_statement_opening_and_running_balance(self):
        response = self.client.get("/reports/party-statement?kind=supplier&party_id=1&date_from=2026-11-01&date_to=2026-11-30")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("كشف حساب مورد", body)
        self.assertIn("1,700.00", body)

    def test_supplier_statement_includes_supplier_credit_notes(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""INSERT INTO supplier_credit_notes
            (id,credit_no,supplier_id,bill_id,credit_date,reason,total_amount,status)
            VALUES(1,'SCN-1',1,2,'2026-11-25','مرتجع مورد','30.00','posted')""")
        conn.commit()
        conn.close()
        response = self.client.get(
            "/reports/party-statement?kind=supplier&party_id=1&date_from=2026-11-01&date_to=2026-11-30"
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("SCN-1", body)
        self.assertIn("1,670.00", body)


if __name__ == "__main__":
    unittest.main()
