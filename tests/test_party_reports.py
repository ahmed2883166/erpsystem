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

    def test_cancelled_customer_credit_note_and_reversal_net_to_zero(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("ALTER TABLE sales_credit_notes ADD COLUMN cancel_journal_entry_id INTEGER")
        conn.execute("ALTER TABLE sales_credit_notes ADD COLUMN cancellation_reason TEXT")
        conn.execute("CREATE TABLE journal_entries(id INTEGER PRIMARY KEY,entry_date TEXT,status TEXT)")
        conn.execute("INSERT INTO journal_entries VALUES(1,'2026-11-28','posted')")
        conn.execute(
            """INSERT INTO sales_credit_notes(
                id,invoice_id,credit_note_no,credit_date,reason,total_amount,status,
                cancel_journal_entry_id,cancellation_reason
            ) VALUES(2,2,'SCN-CANCEL','2026-11-21','cancelled return','40','cancelled',1,'wrong note')"""
        )
        conn.commit()
        conn.close()

        response = self.client.get(
            "/reports/party-statement?kind=customer&party_id=1&date_from=2026-11-01&date_to=2026-11-30"
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("SCN-CANCEL", body)
        self.assertIn("SCN-CANCEL-CANCEL", body)
        self.assertIn("1,200.00", body)

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

    def test_customer_statement_includes_advance_applications_but_not_unallocated_receipt(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""CREATE TABLE customer_advance_receipts(
            id INTEGER PRIMARY KEY, receipt_no TEXT, customer_id INTEGER,
            receipt_date TEXT, amount TEXT, journal_entry_id INTEGER
        )""")
        conn.execute("""INSERT INTO customer_advance_receipts
            VALUES(1,'CADV-UNALLOCATED',1,'2026-11-23','200.00',98)""")
        conn.execute("""CREATE TABLE customer_advance_allocations(
            id INTEGER PRIMARY KEY, advance_receipt_id INTEGER, invoice_id INTEGER,
            allocation_date TEXT, amount TEXT, journal_entry_id INTEGER
        )""")
        conn.execute(
            "INSERT INTO customer_advance_allocations VALUES(1,1,2,'2026-11-24','50.00',99)"
        )
        conn.commit()
        conn.close()

        response = self.client.get(
            "/reports/party-statement?kind=customer&party_id=1&date_from=2026-11-01&date_to=2026-11-30"
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("CADVA-1", body)
        self.assertNotIn("CADV-UNALLOCATED", body)
        # Opening 800 + current invoice 500 - receipt 100 - advance allocation 50.
        # The unallocated 200 advance is intentionally excluded from the statement.
        self.assertIn("1,150.00", body)

    def test_subledger_reconciliation_matches_customer_supplier_and_advance_controls(self):
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS company_settings(
                setting_key TEXT PRIMARY KEY, setting_value TEXT
            );
            INSERT OR REPLACE INTO company_settings VALUES('activity','commercial');
            CREATE TABLE accounts(
                id INTEGER PRIMARY KEY, code TEXT NOT NULL, account_type TEXT NOT NULL,
                is_group INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO accounts(id,code,account_type) VALUES
                (1,'1130','asset'),(2,'2110','liability'),
                (3,'2160','liability'),(4,'1110','asset'),(5,'3999','equity');
            CREATE TABLE journal_entries(
                id INTEGER PRIMARY KEY, entry_date TEXT NOT NULL, status TEXT NOT NULL
            );
            CREATE TABLE journal_lines(
                id INTEGER PRIMARY KEY, entry_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL, debit TEXT NOT NULL, credit TEXT NOT NULL
            );
            CREATE TABLE customer_receipt_batches(
                id INTEGER PRIMARY KEY, customer_id INTEGER, receipt_no TEXT,
                receipt_date TEXT, total_amount TEXT, reference TEXT
            );
            CREATE TABLE customer_receipt_allocations(
                id INTEGER PRIMARY KEY, batch_id INTEGER, invoice_id INTEGER, amount TEXT
            );
            CREATE TABLE customer_advance_receipts(
                id INTEGER PRIMARY KEY, receipt_no TEXT, customer_id INTEGER,
                receipt_date TEXT, amount TEXT, journal_entry_id INTEGER
            );
            CREATE TABLE customer_advance_allocations(
                id INTEGER PRIMARY KEY, advance_receipt_id INTEGER, invoice_id INTEGER,
                allocation_date TEXT, amount TEXT, journal_entry_id INTEGER
            );
            CREATE TABLE customer_advance_refunds(
                id INTEGER PRIMARY KEY, advance_receipt_id INTEGER,
                refund_date TEXT, amount TEXT, journal_entry_id INTEGER
            );
        """)
        conn.execute(
            "INSERT INTO customer_receipt_batches VALUES(1,1,'RCPTB-1','2026-11-25','150','')"
        )
        conn.execute("INSERT INTO customer_receipt_allocations VALUES(1,1,2,'150')")
        conn.execute(
            "INSERT INTO sales_credit_notes(id,invoice_id,credit_note_no,credit_date,reason,total_amount,status) "
            "VALUES(1,2,'SCN-1','2026-11-21','return','20','posted')"
        )
        conn.execute(
            "INSERT INTO customer_refunds(id,credit_note_id,refund_no,refund_date,amount) "
            "VALUES(1,1,'RF-1','2026-11-22','5')"
        )
        conn.execute(
            "INSERT INTO customer_advance_receipts VALUES(1,'CADV-1',1,'2026-11-23','200',90)"
        )
        conn.execute(
            "INSERT INTO customer_advance_allocations VALUES(1,1,2,'2026-11-24','50',91)"
        )
        conn.execute(
            "INSERT INTO customer_advance_refunds VALUES(1,1,'2026-11-26','20',92)"
        )
        conn.execute(
            "INSERT INTO supplier_credit_notes(id,credit_no,supplier_id,bill_id,credit_date,reason,total_amount,status) "
            "VALUES(1,'SUP-CN-1',1,2,'2026-11-25','return','30','posted')"
        )

        journal_entries = [
            (1,'2026-10-10',[(1,'1000','0')]),
            (2,'2026-11-10',[(1,'500','0')]),
            (3,'2026-10-20',[(1,'0','200')]),
            (4,'2026-11-20',[(1,'0','100')]),
            (5,'2026-11-25',[(1,'0','150')]),
            (6,'2026-11-24',[(1,'0','50')]),
            (7,'2026-11-21',[(1,'0','20')]),
            (8,'2026-11-22',[(1,'5','0')]),
            (9,'2026-10-10',[(2,'0','2000')]),
            (10,'2026-11-10',[(2,'0','400')]),
            (11,'2026-10-20',[(2,'500','0')]),
            (12,'2026-11-20',[(2,'200','0')]),
            (13,'2026-11-25',[(2,'30','0')]),
            (14,'2026-11-23',[(3,'0','200')]),
            (15,'2026-11-24',[(3,'50','0')]),
            (17,'2026-11-26',[(3,'20','0')]),
        ]
        line_id = 1
        for entry_id, entry_date, control_lines in journal_entries:
            conn.execute(
                "INSERT INTO journal_entries(id,entry_date,status) VALUES(?,?,'posted')",
                (entry_id, entry_date),
            )
            for account_id, debit, credit in control_lines:
                conn.execute(
                    "INSERT INTO journal_lines VALUES(?,?,?,?,?)",
                    (line_id, entry_id, account_id, debit, credit),
                )
                line_id += 1
                # Keep each synthetic journal balanced without changing control totals.
                conn.execute(
                    "INSERT INTO journal_lines VALUES(?,?,?,?,?)",
                    (line_id, entry_id, 5, credit, debit),
                )
                line_id += 1
        conn.commit()
        conn.close()

        response = self.client.get(
            "/reports/subledger-reconciliation?as_of=2026-11-30"
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("مطابقة الأستاذ العام", body)
        self.assertIn("متطابق", body)
        self.assertIn("985.00", body)
        self.assertIn("1,670.00", body)
        self.assertIn("130.00", body)

        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO journal_entries(id,entry_date,status) VALUES(16,'2026-11-30','posted')")
        conn.execute("INSERT INTO journal_lines VALUES(99,16,1,'10','0')")
        conn.execute("INSERT INTO journal_lines VALUES(100,16,5,'0','10')")
        conn.commit()
        conn.close()
        mismatch = self.client.get(
            "/reports/subledger-reconciliation?as_of=2026-11-30"
        )
        self.assertEqual(mismatch.status_code, 200)
        self.assertIn("يوجد فرق", mismatch.get_data(as_text=True))

        advances = self.client.get("/reports/customer-advances?as_of=2026-11-30")
        self.assertEqual(advances.status_code, 200)
        advance_body = advances.get_data(as_text=True)
        self.assertIn("200.00", advance_body)
        self.assertIn("50.00", advance_body)
        self.assertIn("20.00", advance_body)
        self.assertIn("130.00", advance_body)

    def test_party_statement_rejects_invalid_or_reversed_date_ranges(self):
        invalid = self.client.get(
            "/reports/party-statement?kind=customer&party_id=1&date_from=not-a-date"
        )
        reversed_range = self.client.get(
            "/reports/party-statement?kind=customer&party_id=1&date_from=2026-11-30&date_to=2026-11-01"
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(reversed_range.status_code, 400)

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



    def test_detailed_party_reconciliation_shows_customer_gl_and_discloses_supplier_dimension_gap(self):
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE company_settings(setting_key TEXT PRIMARY KEY, setting_value TEXT);
            INSERT INTO company_settings VALUES('activity','commercial');
            CREATE TABLE accounts(
                id INTEGER PRIMARY KEY, code TEXT NOT NULL, account_type TEXT NOT NULL,
                is_group INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO accounts(id,code,account_type) VALUES
                (1,'1130','asset'),(2,'2110','liability');
            CREATE TABLE journal_entries(
                id INTEGER PRIMARY KEY, entry_date TEXT NOT NULL, status TEXT NOT NULL
            );
            CREATE TABLE journal_lines(
                id INTEGER PRIMARY KEY, entry_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL, debit TEXT NOT NULL, credit TEXT NOT NULL,
                customer_id INTEGER
            );
            INSERT INTO journal_entries VALUES(1,'2026-11-30','posted');
            INSERT INTO journal_lines VALUES(1,1,1,'1200','0',1);
        """)
        conn.commit()
        conn.close()

        customer = self.client.get(
            "/reports/party-reconciliation?kind=customer&as_of=2026-11-30&party_id=1"
        )
        self.assertEqual(customer.status_code, 200)
        customer_body = customer.get_data(as_text=True)
        self.assertIn("1,200.00", customer_body)
        self.assertIn("متطابق", customer_body)
        self.assertIn("SI-OLD", customer_body)
        self.assertIn("SI-NOW", customer_body)

        # A legacy control-account line without customer_id makes a party-level
        # GL balance incomplete and must not be reported as an exact match.
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO journal_entries VALUES(2,'2026-11-30','posted')")
        conn.execute("INSERT INTO journal_lines VALUES(2,2,1,'10','0',NULL)")
        conn.commit()
        conn.close()
        legacy = self.client.get(
            "/reports/party-reconciliation?kind=customer&as_of=2026-11-30&party_id=1"
        )
        self.assertEqual(legacy.status_code, 200)
        self.assertIn("غير متاح", legacy.get_data(as_text=True))

        supplier = self.client.get(
            "/reports/party-reconciliation?kind=supplier&as_of=2026-11-30&party_id=1"
        )
        self.assertEqual(supplier.status_code, 200)
        supplier_body = supplier.get_data(as_text=True)
        self.assertIn("1,700.00", supplier_body)
        self.assertIn("غير متاح", supplier_body)
        self.assertIn("قيود موردين قديمة", supplier_body)

if __name__ == "__main__":
    unittest.main()
