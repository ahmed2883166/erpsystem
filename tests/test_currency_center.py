import sqlite3
import tempfile
import unittest
from pathlib import Path

from flask import Flask
import currency_center


class CurrencyCenterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "nexus.db"
        self.original_path = currency_center.DB_PATH
        currency_center.DB_PATH = self.db_path
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
          CREATE TABLE accounts(id INTEGER PRIMARY KEY, code TEXT, name_ar TEXT, account_type TEXT,
            is_group INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1);
          CREATE TABLE currencies(code TEXT PRIMARY KEY, name_ar TEXT, active INTEGER NOT NULL DEFAULT 1,
            is_base INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE journal_entries(id INTEGER PRIMARY KEY, entry_date TEXT, reference TEXT,
            description TEXT, status TEXT, created_by INTEGER, created_at TEXT);
          CREATE TABLE journal_lines(id INTEGER PRIMARY KEY, entry_id INTEGER, account_id INTEGER,
            debit TEXT, credit TEXT, memo TEXT);
          INSERT INTO currencies VALUES('USD','دولار',1,0);
          INSERT INTO accounts VALUES(1,'1000','البنك','asset',0,1);
          INSERT INTO accounts VALUES(2,'1100','العملاء','asset',0,1);
          INSERT INTO accounts VALUES(3,'4100','المبيعات','revenue',0,1);
          INSERT INTO accounts VALUES(4,'7100','أرباح فروق عملة','revenue',0,1);
          INSERT INTO accounts VALUES(5,'7200','خسائر فروق عملة','expense',0,1);
        """)
        conn.commit()
        conn.close()
        currency_center.initialize_currency_schema()
        self.app = Flask(__name__)
        self.app.secret_key = "test"
        self.app.register_blueprint(currency_center.currency_bp)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["role"] = "accountant"

    def tearDown(self):
        currency_center.DB_PATH = self.original_path
        self.temp.cleanup()

    def test_receivable_settlement_and_period_end_revaluation(self):
        response = self.client.post("/currencies/transactions", data={
            "party_type": "receivable", "reference": "INV-USD-1", "party_name": "Foreign Customer",
            "currency_code": "USD", "transaction_date": "2026-11-01",
            "foreign_amount": "1000", "rate": "50", "party_account_id": "2",
            "counterpart_account_id": "3"
        })
        self.assertEqual(response.status_code, 302)
        response = self.client.post("/currencies/1/settle", data={
            "settlement_date": "2026-11-05", "foreign_amount": "500", "rate": "51",
            "cash_account_id": "1", "fx_gain_account_id": "4", "fx_loss_account_id": "5"
        })
        self.assertEqual(response.status_code, 302)
        conn = sqlite3.connect(self.db_path)
        state = conn.execute("SELECT balance_foreign,balance_base,status FROM fx_transactions WHERE id=1").fetchone()
        settled_diff = conn.execute("SELECT fx_difference FROM fx_settlements WHERE transaction_id=1").fetchone()[0]
        conn.close()
        self.assertEqual(state, ("500", "25000.00", "open"))
        self.assertEqual(settled_diff, "500.00")
        response = self.client.post("/currencies/1/revalue", data={
            "revaluation_date": "2026-11-30", "rate": "52",
            "fx_gain_account_id": "4", "fx_loss_account_id": "5"
        })
        self.assertEqual(response.status_code, 302)
        conn = sqlite3.connect(self.db_path)
        balance = conn.execute("SELECT balance_base FROM fx_transactions WHERE id=1").fetchone()[0]
        difference = conn.execute("SELECT difference FROM fx_revaluations WHERE transaction_id=1").fetchone()[0]
        rows = conn.execute("SELECT debit,credit FROM journal_lines").fetchall()
        from decimal import Decimal
        debit = sum((Decimal(row[0]) for row in rows), Decimal("0"))
        credit = sum((Decimal(row[1]) for row in rows), Decimal("0"))
        conn.close()
        self.assertEqual(balance, "26000.00")
        self.assertEqual(difference, "1000.00")
        self.assertEqual(debit, credit)

    def test_full_settlement_clears_base_currency_residual(self):
        self.client.post("/currencies/transactions", data={
            "party_type": "receivable", "reference": "INV-USD-FULL", "party_name": "Customer",
            "currency_code": "USD", "transaction_date": "2026-11-01",
            "foreign_amount": "100", "rate": "50.01", "party_account_id": "2",
            "counterpart_account_id": "3"
        })
        response = self.client.post("/currencies/1/settle", data={
            "settlement_date": "2026-11-05", "foreign_amount": "100", "rate": "50.02",
            "cash_account_id": "1", "fx_gain_account_id": "4", "fx_loss_account_id": "5"
        })
        self.assertEqual(response.status_code, 302)
        conn = sqlite3.connect(self.db_path)
        state = conn.execute("SELECT balance_foreign,balance_base,status FROM fx_transactions WHERE id=1").fetchone()
        conn.close()
        self.assertEqual(state, ("0", "0.00", "settled"))

    def test_duplicate_revaluation_is_rejected(self):
        self.client.post("/currencies/transactions", data={
            "party_type": "receivable", "reference": "INV-USD-2", "party_name": "Customer",
            "currency_code": "USD", "transaction_date": "2026-11-01",
            "foreign_amount": "100", "rate": "50", "party_account_id": "2",
            "counterpart_account_id": "3"
        })
        payload = {"revaluation_date": "2026-11-30", "rate": "51",
                   "fx_gain_account_id": "4", "fx_loss_account_id": "5"}
        self.client.post("/currencies/1/revalue", data=payload)
        self.client.post("/currencies/1/revalue", data=payload)
        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM fx_revaluations WHERE transaction_id=1").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
