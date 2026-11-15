import sqlite3
import unittest
from decimal import Decimal

from cash_commitments import (
    initialize_cash_commitments_schema,
    create_cash_commitment,
    record_cash_commitment_payment,
    void_cash_commitment,
    list_cash_commitments_forecast,
    create_recurring_schedule,
    generate_recurring_schedule_occurrences,
    deactivate_recurring_schedule,
    cash_commitment_payment_summary,
    reverse_cash_commitment_payment,
)


class CashCommitmentsTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""CREATE TABLE accounts(
            id INTEGER PRIMARY KEY, account_type TEXT, is_group INTEGER, active INTEGER
        )""")
        self.conn.executemany("INSERT INTO accounts VALUES(?,?,?,?)",[(10,"liability",0,1),(20,"asset",0,1)])
        self.conn.execute("""CREATE TABLE journal_entries(
            id INTEGER PRIMARY KEY, status TEXT NOT NULL,
            reference TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
            entry_date TEXT NOT NULL DEFAULT '2026-11-30'
        )""")
        self.conn.execute("""CREATE TABLE journal_lines(
            id INTEGER PRIMARY KEY, entry_id INTEGER, account_id INTEGER,
            debit TEXT NOT NULL DEFAULT '0', credit TEXT NOT NULL DEFAULT '0'
        )""")
        self.conn.executemany(
            "INSERT INTO journal_entries(id,status) VALUES(?,?)",
            [(1, "posted"), (2, "draft"), (3, "posted"), (4, "posted"), (5, "posted")],
        )
        self.conn.executemany(
            "INSERT INTO journal_lines(entry_id,account_id,debit,credit) VALUES(?,?,?,?)",
            [(1,10,"200","0"),(1,20,"0","200"),
             (3,10,"100","0"),(3,20,"0","100"),
             (4,10,"60","0"),(4,20,"0","60"),
             (5,10,"150","0"),(5,20,"0","150")],
        )
        initialize_cash_commitments_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def _commitment(self, amount="1000", due="2026-11-25"):
        return create_cash_commitment(
            self.conn, "tax", "ضريبة قيمة مضافة", amount, due,
            period_label="2026-10", currency_code="EGP", payable_account_id=10,
        )

    def test_schema_migration_adds_reversal_link_to_legacy_payment_table(self):
        legacy = sqlite3.connect(":memory:")
        legacy.row_factory = sqlite3.Row
        legacy.executescript("""
            CREATE TABLE journal_entries(id INTEGER PRIMARY KEY);
            CREATE TABLE cash_commitments(
                id INTEGER PRIMARY KEY, status TEXT, due_date TEXT
            );
            CREATE TABLE cash_commitment_payments(
                id INTEGER PRIMARY KEY, commitment_id INTEGER,
                payment_date TEXT, amount TEXT, journal_entry_id INTEGER UNIQUE,
                reference TEXT, notes TEXT, created_by INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        initialize_cash_commitments_schema(legacy)
        columns = {
            row["name"] for row in legacy.execute(
                "PRAGMA table_info(cash_commitment_payments)"
            )
        }
        self.assertIn("reversal_journal_entry_id", columns)
        legacy.close()

    def test_schema_initialization_is_idempotent(self):
        initialize_cash_commitments_schema(self.conn)
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='cash_commitments'"
        ).fetchone())

    def test_partial_payments_and_forecast_as_of_date(self):
        commitment_id = self._commitment()
        record_cash_commitment_payment(
            self.conn, commitment_id, "2026-11-10", "200", 1
        )
        record_cash_commitment_payment(
            self.conn, commitment_id, "2026-11-20", "100", 3
        )
        report = list_cash_commitments_forecast(
            self.conn, "2026-11-15", "2026-12-15"
        )
        self.assertEqual(len(report), 1)
        self.assertEqual(report[0]["balance"], Decimal("800.00"))
        status = self.conn.execute(
            "SELECT status FROM cash_commitments WHERE id=?", (commitment_id,)
        ).fetchone()["status"]
        self.assertEqual(status, "partially_paid")

    def test_payment_requires_existing_posted_journal_entry(self):
        commitment_id = self._commitment()
        with self.assertRaises(ValueError):
            record_cash_commitment_payment(
                self.conn, commitment_id, "2026-11-10", "100", 2
            )


    def test_payment_requires_debit_to_the_commitment_liability_account(self):
        commitment_id = self._commitment("100")
        with self.assertRaisesRegex(ValueError, "يجب أن تساوي"):
            record_cash_commitment_payment(
                self.conn, commitment_id, "2026-11-10", "100", 4
            )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM cash_commitment_payments").fetchone()[0],
            0,
        )

    def test_cannot_overpay_or_reuse_journal_entry(self):
        commitment_id = self._commitment("200")
        record_cash_commitment_payment(
            self.conn, commitment_id, "2026-11-10", "60", 4
        )
        with self.assertRaisesRegex(ValueError, "تتجاوز الرصيد المتبقي"):
            record_cash_commitment_payment(
                self.conn, commitment_id, "2026-11-11", "150", 5
            )
        with self.assertRaises(sqlite3.IntegrityError):
            record_cash_commitment_payment(
                self.conn, commitment_id, "2026-11-11", "60", 4
            )

    def test_reversing_payment_restores_commitment_balance_and_preserves_journals(self):
        commitment_id = self._commitment("200")
        payment_id = record_cash_commitment_payment(
            self.conn, commitment_id, "2026-11-10", "200", 1
        )
        self.assertEqual(
            cash_commitment_payment_summary(self.conn, commitment_id)["status"], "paid"
        )

        def fake_post_journal(conn, entry_date, reference, description, lines, user_id):
            reversal_id = conn.execute(
                "SELECT COALESCE(MAX(id),0)+1 FROM journal_entries"
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO journal_entries(id,status,reference,description,entry_date)
                   VALUES(?,'posted',?,?,?)""",
                (reversal_id, reference, description, entry_date),
            )
            for account_id, debit, credit, memo in lines:
                conn.execute(
                    """INSERT INTO journal_lines(entry_id,account_id,debit,credit,memo)
                       VALUES(?,?,?,?,?)""",
                    (reversal_id, account_id, debit, credit, memo),
                )
            return reversal_id

        original_before = tuple(self.conn.execute(
            "SELECT status,reference FROM journal_entries WHERE id=1"
        ).fetchone())
        reversal_id = reverse_cash_commitment_payment(
            self.conn, payment_id, "2026-11-15", "تم تسجيل السداد بالخطأ",
            created_by=9, post_journal_func=fake_post_journal,
        )
        self.assertEqual(reversal_id, 6)
        self.assertEqual(tuple(self.conn.execute(
            "SELECT status,reference FROM journal_entries WHERE id=1"
        ).fetchone()), original_before)
        reversal_lines = self.conn.execute(
            "SELECT account_id,debit,credit FROM journal_lines WHERE entry_id=? ORDER BY id",
            (reversal_id,),
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in reversal_lines],
            [(10, "0", "200"), (20, "200", "0")],
        )
        payment = self.conn.execute(
            "SELECT reversal_journal_entry_id FROM cash_commitment_payments WHERE id=?",
            (payment_id,),
        ).fetchone()
        self.assertEqual(payment["reversal_journal_entry_id"], reversal_id)
        summary = cash_commitment_payment_summary(self.conn, commitment_id)
        self.assertEqual(summary["paid_total"], Decimal("0"))
        self.assertEqual(summary["status"], "open")
        forecast = list_cash_commitments_forecast(
            self.conn, "2026-11-15", "2026-12-15"
        )
        self.assertEqual(forecast[0]["balance"], Decimal("200.00"))
        with self.assertRaisesRegex(ValueError, "بالفعل"):
            reverse_cash_commitment_payment(
                self.conn, payment_id, "2026-11-16", "محاولة عكس ثانية",
                post_journal_func=fake_post_journal,
            )

    def test_reversed_or_reversal_journal_cannot_be_linked_as_payment(self):
        commitment_id = self._commitment("200")
        self.conn.execute(
            "INSERT INTO journal_entries(id,status,reference,description) VALUES(6,'posted','REV-1','قيد عكسي')"
        )
        self.conn.execute(
            "INSERT INTO journal_lines(entry_id,account_id,debit,credit) VALUES(6,10,'200','0')"
        )
        with self.assertRaisesRegex(ValueError, "قيد عكسي"):
            record_cash_commitment_payment(
                self.conn, commitment_id, "2026-11-10", "200", 6
            )
        self.conn.execute(
            "INSERT INTO journal_entries(id,status,reference,description) VALUES(7,'posted','REV-4','عكس قيد السداد')"
        )
        with self.assertRaisesRegex(ValueError, "تم عكسه"):
            record_cash_commitment_payment(
                self.conn, commitment_id, "2026-11-10", "60", 4
            )

    def test_paid_commitment_is_not_forecast_and_void_is_excluded(self):
        paid_id = self._commitment("200")
        record_cash_commitment_payment(
            self.conn, paid_id, "2026-11-10", "200", 1
        )
        void_id = self._commitment("70", "2026-11-26")
        void_cash_commitment(self.conn, void_id)
        self.assertEqual(
            list_cash_commitments_forecast(self.conn, "2026-11-15", "2026-12-15"),
            [],
        )

    def test_future_dated_full_payment_does_not_hide_commitment_as_of_earlier_date(self):
        commitment_id = self._commitment("200")
        record_cash_commitment_payment(
            self.conn, commitment_id, "2026-11-20", "200", 1
        )
        report = list_cash_commitments_forecast(
            self.conn, "2026-11-15", "2026-12-15"
        )
        self.assertEqual(len(report), 1)
        self.assertEqual(report[0]["balance"], Decimal("100.00"))
        report_after_payment = list_cash_commitments_forecast(
            self.conn, "2026-11-21", "2026-12-15"
        )
        self.assertEqual(report_after_payment, [])


    def test_recurring_schedule_generates_month_end_dates_once_only(self):
        schedule_id = create_recurring_schedule(
            self.conn, "إيجار المقر", "5000", "2026-01-31", "monthly",
            currency_code="EGP", payable_account_id=10,
        )
        self.assertEqual(
            generate_recurring_schedule_occurrences(self.conn, schedule_id, "2026-03-31"),
            3,
        )
        self.assertEqual(
            generate_recurring_schedule_occurrences(self.conn, schedule_id, "2026-03-31"),
            0,
        )
        self.assertEqual(
            generate_recurring_schedule_occurrences(self.conn, schedule_id, "2026-04-30"),
            1,
        )
        rows = self.conn.execute(
            """SELECT c.due_date,c.amount FROM cash_commitment_schedule_occurrences o
               JOIN cash_commitments c ON c.id=o.commitment_id
               WHERE o.schedule_id=? ORDER BY c.due_date""",
            (schedule_id,),
        ).fetchall()
        self.assertEqual(
            [row["due_date"] for row in rows],
            ["2026-01-31", "2026-02-28", "2026-03-31", "2026-04-30"],
        )
        self.assertEqual([row["amount"] for row in rows], ["5000.00"] * 4)

    def test_deactivating_schedule_preserves_existing_commitments(self):
        schedule_id = create_recurring_schedule(
            self.conn, "اشتراك سنوي", "1200", "2026-01-15", "annual",
            currency_code="EGP", payable_account_id=10,
        )
        self.assertEqual(
            generate_recurring_schedule_occurrences(self.conn, schedule_id, "2026-01-15"),
            1,
        )
        deactivate_recurring_schedule(self.conn, schedule_id)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM cash_commitments").fetchone()[0],
            1,
        )
        with self.assertRaises(ValueError):
            generate_recurring_schedule_occurrences(self.conn, schedule_id, "2027-01-15")

    def test_duplicate_source_reference_is_rejected(self):
        create_cash_commitment(
            self.conn, "friday_overtime", "بدل جمعة", "150", "2026-11-30", payable_account_id=10,
            origin_type="friday_allowance_batch", origin_id=22,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            create_cash_commitment(
                self.conn, "friday_overtime", "بدل جمعة", "150", "2026-11-30", payable_account_id=10,
                origin_type="friday_allowance_batch", origin_id=22,
            )


    def _legacy_friday_schema(self):
        self.conn.execute("""CREATE TABLE friday_allowance_batches(
            id INTEGER PRIMARY KEY, period TEXT NOT NULL, status TEXT NOT NULL,
            total_amount TEXT NOT NULL, payable_account_id INTEGER, journal_entry_id INTEGER
        )""")
        self.conn.execute("""CREATE TABLE company_settings(
            setting_key TEXT PRIMARY KEY, setting_value TEXT
        )""")
        self.conn.execute("INSERT INTO company_settings VALUES('base_currency','EGP')")
        self.conn.execute("""INSERT INTO friday_allowance_batches
            VALUES(1,'2026-01','posted','1250.00',10,1)""")
        self.conn.execute("UPDATE journal_entries SET reference='FRIDAY-ALLOWANCE-2026-01' WHERE id=1")
        self.conn.commit()

    def test_sync_imports_legacy_posted_friday_batch_once_without_gl_changes(self):
        from cash_commitments import sync_friday_allowance_commitments
        self._legacy_friday_schema()
        before_entries = self.conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0]
        before_batches = tuple(self.conn.execute(
            "SELECT id,status,total_amount,journal_entry_id FROM friday_allowance_batches"
        ).fetchone())
        result = sync_friday_allowance_commitments(self.conn, created_by=7)
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["skipped"], [])
        row = self.conn.execute("""SELECT commitment_type,title,amount,due_date,currency_code,
            payable_account_id,origin_type,origin_id,created_by FROM cash_commitments""").fetchone()
        self.assertEqual(tuple(row), ("friday_overtime","بدل الجمعة - 2026-01","1250.00",
            "2026-01-31","EGP",10,"friday_allowance_batch",1,7))
        again = sync_friday_allowance_commitments(self.conn, created_by=7)
        self.assertEqual(again["imported"], 0)
        self.assertEqual(again["skipped"][0]["reason"], "مرتبط بالفعل")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM cash_commitments").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0], before_entries)
        self.assertEqual(tuple(self.conn.execute(
            "SELECT id,status,total_amount,journal_entry_id FROM friday_allowance_batches"
        ).fetchone()), before_batches)

    def test_sync_skips_reversed_batch_but_imports_restored_reversal_chain(self):
        from cash_commitments import sync_friday_allowance_commitments
        self._legacy_friday_schema()
        self.conn.execute(
            "INSERT INTO journal_entries(id,status,reference,description) VALUES(5,'posted','REV-1','reversal')"
        )
        result = sync_friday_allowance_commitments(self.conn)
        self.assertEqual(result["imported"], 0)
        self.assertEqual(result["skipped"][0]["reason"], "قيد الاستحقاق معكوس")
        self.conn.execute(
            "INSERT INTO journal_entries(id,status,reference,description) VALUES(6,'posted','REV-5','restoration')"
        )
        result = sync_friday_allowance_commitments(self.conn)
        self.assertEqual(result["imported"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM cash_commitments").fetchone()[0], 1)

    def test_sync_reports_invalid_payable_account_and_unposted_journal(self):
        from cash_commitments import sync_friday_allowance_commitments
        self._legacy_friday_schema()
        self.conn.execute(
            "INSERT INTO friday_allowance_batches VALUES(2,'2026-02','posted','900',20,3)"
        )
        self.conn.execute(
            "INSERT INTO friday_allowance_batches VALUES(3,'2026-03','posted','900',10,2)"
        )
        result = sync_friday_allowance_commitments(self.conn)
        self.assertEqual(result["imported"], 1)
        self.assertEqual(len(result["skipped"]), 2)
        reasons = {item["reason"] for item in result["skipped"]}
        self.assertIn("حساب الالتزام يجب أن يكون حساب خصوم تفصيليًا ونشطًا.", reasons)
        self.assertIn("قيد الاستحقاق غير موجود أو غير مرحّل", reasons)



if __name__ == "__main__":
    unittest.main()
