import sqlite3
import unittest

from analytic_dimensions import initialize_analytic_dimensions_schema, validate_journal_dimensions
from accounting_core import post_journal
from management_dashboard import account_balances


class AnalyticDimensionsTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE accounts(id INTEGER PRIMARY KEY, code TEXT, name_ar TEXT,
                account_type TEXT, is_group INTEGER DEFAULT 0, active INTEGER DEFAULT 1);
            CREATE TABLE customers(id INTEGER PRIMARY KEY,code TEXT,name TEXT,active INTEGER DEFAULT 1);
            CREATE TABLE salespersons(id INTEGER PRIMARY KEY,code TEXT,name TEXT,active INTEGER DEFAULT 1);
            INSERT INTO customers(id,code,name) VALUES(1,'C001','عميل تجريبي'),(2,'C002','عميل آخر');
            INSERT INTO salespersons(id,code,name) VALUES(1,'SP01','مندوب أول'),(2,'SP02','مندوب ثانٍ');
            CREATE TABLE journal_entries(id INTEGER PRIMARY KEY, entry_date TEXT NOT NULL,
                reference TEXT, description TEXT NOT NULL, status TEXT DEFAULT 'posted',
                created_by INTEGER, created_at TEXT NOT NULL);
            CREATE TABLE journal_lines(id INTEGER PRIMARY KEY,
                entry_id INTEGER REFERENCES journal_entries(id), account_id INTEGER,
                debit TEXT NOT NULL DEFAULT '0', credit TEXT NOT NULL DEFAULT '0', memo TEXT);
            INSERT INTO accounts(id,code,name_ar,account_type) VALUES
                (1,'1110','الصندوق','asset'),(2,'4100','المبيعات','revenue');
        """)
        initialize_analytic_dimensions_schema(self.conn)
        self.conn.execute("INSERT INTO branches(code,name_ar) VALUES('CAI','فرع القاهرة')")
        self.branch_id = self.conn.execute("SELECT id FROM branches").fetchone()["id"]

    def tearDown(self):
        self.conn.close()

    def test_manual_journal_saves_selected_dimensions(self):
        dimensions = validate_journal_dimensions(self.conn, {
            "branch_id": str(self.branch_id), "cost_center_id": "", "project_id": ""
        })
        entry_id = post_journal(
            self.conn, "2026-11-01", "DIM-001", "قيد اختبار",
            [(1, "100", "0", "مدين"), (2, "0", "100", "دائن")],
            1, dimensions,
        )
        rows = self.conn.execute(
            "SELECT branch_id,cost_center_id,project_id FROM journal_lines WHERE entry_id=?",
            (entry_id,),
        ).fetchall()
        self.assertEqual([row["branch_id"] for row in rows], [self.branch_id, self.branch_id])
        self.assertTrue(all(row["cost_center_id"] is None and row["project_id"] is None for row in rows))

    def test_dashboard_account_balances_filter_by_branch(self):
        post_journal(
            self.conn, "2026-11-01", "DIM-002", "قيد فرع",
            [(1, "100", "0", "مدين"), (2, "0", "100", "دائن")],
            1, {"branch_id": self.branch_id},
        )
        post_journal(
            self.conn, "2026-11-02", "DIM-003", "قيد دون فرع",
            [(1, "50", "0", "مدين"), (2, "0", "50", "دائن")],
            1,
        )
        all_rows = account_balances(self.conn, "2026-11-01", "2026-11-30")
        branch_rows = account_balances(
            self.conn, "2026-11-01", "2026-11-30", {"branch_id": self.branch_id}
        )
        self.assertEqual(all_rows[1]["debit"], 150)
        self.assertEqual(branch_rows[1]["debit"], 100)


    def test_customer_dimension_filters_sales_journal(self):
        customer_id = 1
        dimensions = validate_journal_dimensions(self.conn, {"customer_id": str(customer_id)})
        entry_id = post_journal(
            self.conn, "2026-11-03", "DIM-CUST-001", "قيد عميل",
            [(1, "75", "0", "مدين"), (2, "0", "75", "دائن")],
            1, dimensions,
        )
        rows = self.conn.execute("SELECT customer_id FROM journal_lines WHERE entry_id=?", (entry_id,)).fetchall()
        self.assertTrue(all(row["customer_id"] == customer_id for row in rows))
        filtered = account_balances(self.conn, "2026-11-01", "2026-11-30", {"customer_id": customer_id})
        self.assertEqual(filtered[1]["debit"], 75)


    def test_salesperson_dimension_filters_dashboard(self):
        dimensions = validate_journal_dimensions(self.conn, {"salesperson_id": "2"})
        entry_id = post_journal(
            self.conn, "2026-11-04", "DIM-SP-001", "قيد مندوب",
            [(1, "125", "0", "مدين"), (2, "0", "125", "دائن")],
            1, dimensions,
        )
        rows = self.conn.execute("SELECT salesperson_id FROM journal_lines WHERE entry_id=?", (entry_id,)).fetchall()
        self.assertTrue(all(row["salesperson_id"] == 2 for row in rows))
        filtered = account_balances(self.conn, "2026-11-01", "2026-11-30", {"salesperson_id": 2})
        self.assertEqual(filtered[1]["debit"], 125)

    def test_invalid_or_inactive_dimension_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_journal_dimensions(self.conn, {"branch_id": "999"})

    def test_idempotency_compares_analytical_dimensions(self):
        second = self.conn.execute(
            "INSERT INTO branches(code,name_ar) VALUES('ALX','فرع الإسكندرية')"
        ).lastrowid
        lines = [(1, "100", "0", "مدين"), (2, "0", "100", "دائن")]
        first_id = post_journal(
            self.conn, "2026-11-05", "DIM-IDEMPOTENT", "قيد متعدد الأبعاد",
            lines, 1, {"branch_id": self.branch_id},
        )
        repeated_id = post_journal(
            self.conn, "2026-11-05", "DIM-IDEMPOTENT", "قيد متعدد الأبعاد",
            lines, 1, {"branch_id": self.branch_id},
        )
        self.assertEqual(first_id, repeated_id)
        with self.assertRaisesRegex(ValueError, "مرجع القيد مستخدم بالفعل"):
            post_journal(
                self.conn, "2026-11-05", "DIM-IDEMPOTENT", "قيد متعدد الأبعاد",
                lines, 1, {"branch_id": second},
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM journal_entries WHERE reference='DIM-IDEMPOTENT'"
            ).fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
