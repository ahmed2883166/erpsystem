import sqlite3
import unittest
import fixed_assets
from decimal import Decimal
from unittest.mock import patch

from flask import Flask, session
from fixed_assets import (_asset_disposal_journal_lines, _asset_period_amount,
                          _post_asset_depreciation, _reverse_full_period_depreciation_for_disposal,
                          _validate_dedicated_accum_account, require,
                          register_assets_from_journal_lines, _manual_asset_journal_lines)



class ManualAssetAcquisitionJournalTests(unittest.TestCase):
    def test_manual_asset_creation_builds_one_balanced_cost_journal(self):
        lines = _manual_asset_journal_lines(10, 20, Decimal("12500.50"), "سيارة نقل")
        self.assertEqual(lines[0][:3], (10, Decimal("12500.50"), Decimal("0")))
        self.assertEqual(lines[1][:3], (20, Decimal("0"), Decimal("12500.50")))
        self.assertEqual(sum((line[1] for line in lines), Decimal("0")),
                         sum((line[2] for line in lines), Decimal("0")))

    def test_manual_asset_journal_rejects_same_asset_and_counterpart_account(self):
        with self.assertRaisesRegex(ValueError, "غير صحيحة"):
            _manual_asset_journal_lines(10, 10, Decimal("100"), "أصل")
        with self.assertRaisesRegex(ValueError, "غير صحيحة"):
            _manual_asset_journal_lines(10, 20, Decimal("0"), "أصل")


class FixedAssetPermissionFallbackTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = "test-only"
        self.app.add_url_rule("/login", "login", lambda: "login")
        self.app.add_url_rule("/dashboard", "dashboard", lambda: "dashboard")
        self.app.add_url_rule("/assets/", "assets.index", lambda: "assets")

    def test_legacy_viewer_cannot_post_asset_creation_without_permissions_list(self):
        @require()
        def protected_asset_create():
            return "created"

        self.app.add_url_rule("/assets/create-test", "asset_create_test", protected_asset_create,
                              methods=["POST"])
        with self.app.test_request_context("/assets/create-test", method="POST"):
            session["user_id"] = 7
            session["role"] = "viewer"
            response = protected_asset_create()
            self.assertEqual(response.status_code, 302)
            self.assertIn("/assets/", response.location)

    def test_legacy_accountant_can_post_asset_creation_without_permissions_list(self):
        @require()
        def protected_asset_create():
            return "created"

        self.app.add_url_rule("/assets/create-test", "asset_create_test", protected_asset_create,
                              methods=["POST"])
        with self.app.test_request_context("/assets/create-test", method="POST"):
            session["user_id"] = 7
            session["role"] = "accountant"
            self.assertEqual(protected_asset_create(), "created")


class FixedAssetDepreciationCalculationTests(unittest.TestCase):
    def setUp(self):
        self.asset = {
            "id": 1,
            "asset_code": "M-001",
            "asset_name": "Machine",
            "ready_date": "2026-01-16",
            "cost": "120000",
            "residual_value": "12000",
            "useful_life_months": 60,
            "depreciation_method": "straight_line",
            "annual_rate": "0",
        }

    def test_straight_line_full_month(self):
        amount = _asset_period_amount(self.asset, Decimal("0"), "2026-02")
        self.assertEqual(amount, Decimal("1800.00"))

    def test_straight_line_first_month_is_prorated_from_ready_date(self):
        amount = _asset_period_amount(self.asset, Decimal("0"), "2026-01")
        self.assertEqual(amount, Decimal("929.03"))

    def test_disposal_month_is_prorated_through_disposal_date(self):
        amount = _asset_period_amount(
            self.asset, Decimal("0"), "2026-01", cutoff_date="2026-01-20"
        )
        self.assertEqual(amount, Decimal("290.32"))

    def test_final_partial_month_clears_only_remaining_depreciable_amount(self):
        accumulated = Decimal("107129.03")
        amount = _asset_period_amount(self.asset, accumulated, "2031-01")
        self.assertEqual(amount, Decimal("870.97"))
        self.assertEqual(accumulated + amount, Decimal("108000.00"))

    def test_depreciation_never_exceeds_remaining_depreciable_base(self):
        amount = _asset_period_amount(self.asset, Decimal("107999.99"), "2026-02")
        self.assertEqual(amount, Decimal("0.01"))

    def test_declining_balance_uses_annual_rate_and_monthly_period(self):
        declining = dict(self.asset, depreciation_method="declining_balance", annual_rate="20")
        amount = _asset_period_amount(declining, Decimal("0"), "2026-02")
        self.assertEqual(amount, Decimal("2000.00"))


    def test_posting_creates_one_record_and_dates_journal_at_month_end(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE asset_depreciations(
            id INTEGER PRIMARY KEY, asset_id INTEGER, period TEXT, amount TEXT,
            journal_entry_id INTEGER UNIQUE, UNIQUE(asset_id,period)
        )""")
        asset = dict(self.asset, id=7, ready_date="2026-01-01",
                     cost="1200", residual_value="0", useful_life_months=12,
                     asset_account_id=10, accum_account_id=11, expense_account_id=12)
        with patch("fixed_assets.post_journal", return_value=55) as post:
            amount, entry_id = _post_asset_depreciation(conn, asset, "2026-01", 9)
        self.assertEqual(amount, Decimal("100.00"))
        self.assertEqual(entry_id, 55)
        self.assertEqual(post.call_args.args[1], "2026-01-31")
        journal_lines = post.call_args.args[4]
        self.assertEqual(journal_lines[0][0], 12)
        self.assertEqual(journal_lines[0][1:3], (Decimal("100.00"), 0))
        self.assertEqual(journal_lines[1][0], 11)
        self.assertEqual(journal_lines[1][1:3], (0, Decimal("100.00")))
        row = conn.execute(
            "SELECT period,amount,journal_entry_id FROM asset_depreciations"
        ).fetchone()
        self.assertEqual((row["period"], row["amount"], row["journal_entry_id"]),
                         ("2026-01", "100.00", 55))
        conn.close()

    def test_posting_rejects_a_gap_in_the_depreciation_sequence(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE asset_depreciations(
            id INTEGER PRIMARY KEY, asset_id INTEGER, period TEXT, amount TEXT,
            journal_entry_id INTEGER UNIQUE, UNIQUE(asset_id,period)
        )""")
        asset = dict(self.asset, id=8, ready_date="2026-01-01",
                     cost="1200", residual_value="0", useful_life_months=12,
                     asset_account_id=10, accum_account_id=11, expense_account_id=12)
        with patch("fixed_assets.post_journal") as post:
            with self.assertRaisesRegex(ValueError, "الفترة السابقة"):
                _post_asset_depreciation(conn, asset, "2026-02", 9)
        post.assert_not_called()
        conn.close()

    def test_dedicated_accumulated_depreciation_account_rejects_reuse(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE fixed_assets(id INTEGER PRIMARY KEY, accum_account_id INTEGER);
            CREATE TABLE journal_lines(id INTEGER PRIMARY KEY, account_id INTEGER);
        """)
        _validate_dedicated_accum_account(conn, 22)
        conn.execute("INSERT INTO fixed_assets(accum_account_id) VALUES(22)")
        with self.assertRaisesRegex(ValueError, "مستخدم لأصل آخر"):
            _validate_dedicated_accum_account(conn, 22)
        conn.execute("DELETE FROM fixed_assets")
        conn.execute("INSERT INTO journal_lines(account_id) VALUES(22)")
        with self.assertRaisesRegex(ValueError, "حركات سابقة"):
            _validate_dedicated_accum_account(conn, 22)
        conn.close()

    def test_period_before_ready_date_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "قبل جاهزية"):
            _asset_period_amount(self.asset, Decimal("0"), "2025-12")

    def test_period_after_useful_life_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "انتهى العمر"):
            _asset_period_amount(self.asset, Decimal("0"), "2031-02")




class FixedAssetDisposalJournalTests(unittest.TestCase):
    def setUp(self):
        self.asset = {"asset_name": "سيارة نقل", "asset_account_id": 10, "accum_account_id": 11}

    def test_sale_with_gain_and_output_vat_posts_cash_asset_accumulated_and_gain(self):
        lines = _asset_disposal_journal_lines(
            self.asset, Decimal("100000"), Decimal("30000"), Decimal("92000"),
            Decimal("12000"), 20, 30, 40, Decimal("10000")
        )
        self.assertEqual([line[:3] for line in lines], [
            (20, Decimal("92000"), 0), (11, Decimal("30000"), 0),
            (10, 0, Decimal("100000")), (30, 0, Decimal("12000")),
            (40, 0, Decimal("10000")),
        ])
        debit = sum((Decimal(str(line[1])) for line in lines), Decimal("0"))
        credit = sum((Decimal(str(line[2])) for line in lines), Decimal("0"))
        self.assertEqual(debit, credit)

    def test_disposal_without_proceeds_does_not_create_zero_cash_line(self):
        lines = _asset_disposal_journal_lines(
            self.asset, Decimal("100000"), Decimal("30000"), Decimal("0"),
            Decimal("0"), 20, None, 40, Decimal("-70000")
        )
        self.assertEqual([line[0] for line in lines], [11, 10, 40])
        debit = sum((Decimal(str(line[1])) for line in lines), Decimal("0"))
        credit = sum((Decimal(str(line[2])) for line in lines), Decimal("0"))
        self.assertEqual(debit, credit)

    def test_sale_at_loss_is_balanced_and_records_loss_as_debit(self):
        lines = _asset_disposal_journal_lines(
            self.asset, Decimal("100000"), Decimal("30000"), Decimal("60000"),
            Decimal("0"), 20, None, 40, Decimal("-10000")
        )
        self.assertEqual(lines[-1][:3], (40, Decimal("10000"), 0))
        self.assertEqual(sum((Decimal(str(x[1])) for x in lines), Decimal("0")),
                         sum((Decimal(str(x[2])) for x in lines), Decimal("0")))


class FixedAssetDepreciationRevisionTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE asset_depreciations(
                id INTEGER PRIMARY KEY, asset_id INTEGER, period TEXT, amount TEXT,
                journal_entry_id INTEGER UNIQUE, posted_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(asset_id,period)
            );
            CREATE TABLE journal_entries(
                id INTEGER PRIMARY KEY, reference TEXT, status TEXT, entry_date TEXT
            );
            CREATE TABLE journal_lines(
                id INTEGER PRIMARY KEY, entry_id INTEGER, account_id INTEGER,
                debit TEXT, credit TEXT, memo TEXT
            );
            CREATE TABLE asset_depreciation_revisions(
                id INTEGER PRIMARY KEY, depreciation_id INTEGER UNIQUE,
                original_journal_entry_id INTEGER, reversal_journal_entry_id INTEGER UNIQUE,
                replacement_journal_entry_id INTEGER UNIQUE, original_amount TEXT,
                replacement_amount TEXT, disposal_date TEXT, reason TEXT, created_by INTEGER
            );
            INSERT INTO asset_depreciations(id,asset_id,period,amount,journal_entry_id)
                VALUES(1,1,'2026-01','100.00',9),(2,1,'2026-02','100.00',10);
            INSERT INTO journal_entries VALUES(10,'DEP-1-2026-02','posted','2026-02-28');
            INSERT INTO journal_lines VALUES(1,10,30,'100.00','0','مصروف إهلاك');
            INSERT INTO journal_lines VALUES(2,10,40,'0','100.00','مجمع إهلاك');
        """)
        self.asset = {
            "id": 1, "asset_code": "AST-001", "asset_name": "سيارة",
            "ready_date": "2026-01-01", "cost": "1200",
            "residual_value": "0", "useful_life_months": 12,
            "depreciation_method": "straight_line", "annual_rate": "0",
            "expense_account_id": 30, "accum_account_id": 40,
        }

    def tearDown(self):
        self.conn.close()

    def test_full_month_posting_is_reversed_and_replaced_by_daily_amount(self):
        with patch("fixed_assets.post_journal", side_effect=[77, 78]) as post:
            result = _reverse_full_period_depreciation_for_disposal(
                self.conn, self.asset, "2026-02", "2026-02-15", 5
            )
        self.assertEqual(result, (Decimal("53.57"), 77, 78))
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].args[2], "ASSET-DEP-REV-2")
        self.assertEqual(post.call_args_list[0].args[4], [
            (30, "0", "100.00", "عكس إهلاك أصل قبل إعادة احتسابه: مصروف إهلاك"),
            (40, "100.00", "0", "عكس إهلاك أصل قبل إعادة احتسابه: مجمع إهلاك"),
        ])
        row = self.conn.execute(
            "SELECT amount,journal_entry_id FROM asset_depreciations WHERE id=2"
        ).fetchone()
        self.assertEqual((row["amount"], row["journal_entry_id"]), ("53.57", 78))
        revision = self.conn.execute(
            "SELECT original_journal_entry_id,reversal_journal_entry_id,"
            "replacement_journal_entry_id,original_amount,replacement_amount "
            "FROM asset_depreciation_revisions WHERE depreciation_id=2"
        ).fetchone()
        self.assertEqual(tuple(revision), (10, 77, 78, "100.00", "53.57"))

    def test_disposal_on_last_day_keeps_full_month_depreciation_without_reversal(self):
        with patch("fixed_assets.post_journal") as post:
            result = _reverse_full_period_depreciation_for_disposal(
                self.conn, self.asset, "2026-02", "2026-02-28", 5
            )
        self.assertIsNone(result)
        post.assert_not_called()
        row = self.conn.execute(
            "SELECT amount,journal_entry_id FROM asset_depreciations WHERE id=2"
        ).fetchone()
        self.assertEqual((row["amount"], row["journal_entry_id"]), ("100.00", 10))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM asset_depreciation_revisions").fetchone()[0],
            0,
        )

    def test_later_posted_period_blocks_reversal_without_creating_journal(self):
        self.conn.execute(
            "INSERT INTO asset_depreciations(asset_id,period,amount,journal_entry_id) "
            "VALUES(1,'2026-03','100.00',11)"
        )
        with patch("fixed_assets.post_journal") as post:
            with self.assertRaisesRegex(ValueError, "إهلاك لاحق مرحّل"):
                _reverse_full_period_depreciation_for_disposal(
                    self.conn, self.asset, "2026-02", "2026-02-15", 5
                )
        post.assert_not_called()
        self.assertEqual(
            self.conn.execute("SELECT amount FROM asset_depreciations WHERE id=2").fetchone()[0],
            "100.00",
        )


    def test_mid_month_march_disposal_reverses_full_month_and_posts_inclusive_proration(self):
        # January and February are already depreciated; March was posted as a full month.
        self.conn.execute(
            "INSERT INTO asset_depreciations(asset_id,period,amount,journal_entry_id) "
            "VALUES(1,'2026-03','100.00',11)"
        )
        self.conn.execute(
            "INSERT INTO journal_entries VALUES(11,'DEP-1-2026-03','posted','2026-03-31')"
        )
        self.conn.executemany(
            "INSERT INTO journal_lines(entry_id,account_id,debit,credit,memo) "
            "VALUES(11,?,?,?,?)",
            [
                (30, "100.00", "0", "مصروف إهلاك"),
                (40, "0", "100.00", "مجمع إهلاك"),
            ],
        )

        with patch("fixed_assets.post_journal", side_effect=[77, 78]) as post:
            result = _reverse_full_period_depreciation_for_disposal(
                self.conn, self.asset, "2026-03", "2026-03-15", 5
            )

        # 15 inclusive days out of 31: 100 * 15 / 31 = 48.39.
        self.assertEqual(result, (Decimal("48.39"), 77, 78))
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].args[1], "2026-03-15")
        self.assertEqual(post.call_args_list[0].args[2], "ASSET-DEP-REV-3")
        self.assertEqual(post.call_args_list[0].args[4], [
            (30, "0", "100.00", "عكس إهلاك أصل قبل إعادة احتسابه: مصروف إهلاك"),
            (40, "100.00", "0", "عكس إهلاك أصل قبل إعادة احتسابه: مجمع إهلاك"),
        ])
        replacement = post.call_args_list[1]
        self.assertEqual(replacement.args[1], "2026-03-15")
        self.assertEqual(replacement.args[2], "DEP-1-2026-03-DISPOSAL-2026-03-15")
        self.assertEqual(replacement.args[4][0][1:3], (Decimal("48.39"), 0))
        self.assertEqual(replacement.args[4][1][1:3], (0, Decimal("48.39")))

        row = self.conn.execute(
            "SELECT amount,journal_entry_id FROM asset_depreciations "
            "WHERE asset_id=1 AND period='2026-03'"
        ).fetchone()
        self.assertEqual((row["amount"], row["journal_entry_id"]), ("48.39", 78))

        # The audited revision preserves the original, reversal and replacement links.
        revision = self.conn.execute(
            "SELECT original_journal_entry_id,reversal_journal_entry_id,"
            "replacement_journal_entry_id,original_amount,replacement_amount,disposal_date "
            "FROM asset_depreciation_revisions WHERE depreciation_id=3"
        ).fetchone()
        self.assertEqual(tuple(revision), (11, 77, 78, "100.00", "48.39", "2026-03-15"))

        # Accumulated depreciation is Jan + Feb + prorated March, not a full March.
        accumulated = sum(
            (Decimal(row["amount"]) for row in self.conn.execute(
                "SELECT amount FROM asset_depreciations WHERE asset_id=1"
            )),
            Decimal("0"),
        )
        self.assertEqual(accumulated, Decimal("248.39"))
        self.assertEqual(Decimal(self.asset["cost"]) - accumulated, Decimal("951.61"))
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM asset_depreciations WHERE asset_id=1 AND period>'2026-03'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM asset_depreciation_revisions WHERE depreciation_id=3"
            ).fetchone()[0],
            1,
        )


class JournalLinkedAssetRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE journal_entries(id INTEGER PRIMARY KEY, entry_date TEXT, status TEXT);
            CREATE TABLE accounts(id INTEGER PRIMARY KEY, account_type TEXT, is_group INTEGER, active INTEGER);
            CREATE TABLE journal_lines(id INTEGER PRIMARY KEY, entry_id INTEGER, account_id INTEGER,
                debit TEXT, credit TEXT);
            CREATE TABLE fixed_asset_journal_lines(
                journal_line_id INTEGER PRIMARY KEY, entry_id INTEGER, asset_name TEXT,
                category TEXT, ready_date TEXT, cost TEXT, status TEXT, fixed_asset_id INTEGER
            );
            INSERT INTO journal_entries VALUES(40,'2026-11-14','posted');
            INSERT INTO accounts VALUES(10,'asset',0,1);
            INSERT INTO journal_lines VALUES(100,40,10,'25000.00','0');
        """)

    def tearDown(self):
        self.conn.close()

    def test_posted_debit_asset_line_is_staged_at_exact_journal_cost(self):
        result = register_assets_from_journal_lines(
            self.conn, 40,
            [{"asset_name":"سيارة نقل","category":"سيارات","ready_date":"2026-11-20"}],
            7,
        )
        row = self.conn.execute(
            "SELECT * FROM fixed_asset_journal_lines WHERE journal_line_id=100"
        ).fetchone()
        self.assertEqual(result, [100])
        self.assertEqual(row["cost"], "25000.00")
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["entry_id"], 40)

    def test_credit_line_cannot_be_registered_as_fixed_asset(self):
        self.conn.execute(
            "UPDATE journal_lines SET debit='0',credit='25000' WHERE id=100"
        )
        with self.assertRaisesRegex(ValueError, "مدينًا فقط"):
            register_assets_from_journal_lines(
                self.conn, 40,
                [{"asset_name":"سيارة نقل","ready_date":"2026-11-20"}],
                7,
            )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM fixed_asset_journal_lines").fetchone()[0],
            0,
        )

    def test_inactive_or_non_asset_account_is_rejected(self):
        self.conn.execute("UPDATE accounts SET account_type='expense' WHERE id=10")
        with self.assertRaisesRegex(ValueError, "حساب أصل ثابت"):
            register_assets_from_journal_lines(
                self.conn, 40,
                [{"asset_name":"سيارة نقل","ready_date":"2026-11-20"}],
                7,
            )


    def test_reconciliation_aggregates_shared_cost_account_once_across_assets(self):
        import fixed_assets
        conn=sqlite3.connect(":memory:")
        conn.row_factory=sqlite3.Row
        conn.executescript("""
            CREATE TABLE fixed_assets(id INTEGER PRIMARY KEY,asset_account_id INTEGER,accum_account_id INTEGER,cost TEXT,purchase_date TEXT,status TEXT);
            CREATE TABLE asset_disposals(id INTEGER PRIMARY KEY,asset_id INTEGER,disposal_date TEXT);
            CREATE TABLE asset_depreciations(id INTEGER PRIMARY KEY,asset_id INTEGER,amount TEXT,journal_entry_id INTEGER);
            CREATE TABLE journal_entries(id INTEGER PRIMARY KEY,entry_date TEXT,status TEXT);
            CREATE TABLE journal_lines(id INTEGER PRIMARY KEY,entry_id INTEGER,account_id INTEGER,debit TEXT,credit TEXT);
            CREATE TABLE accounts(id INTEGER PRIMARY KEY,code TEXT,name_ar TEXT);
            INSERT INTO accounts VALUES(10,'1500','الأصول'),(11,'1591','مجمع الأصل الأول'),(12,'1592','مجمع الأصل الثاني');
            INSERT INTO fixed_assets VALUES(1,10,11,'1000','2026-01-01','active'),(2,10,12,'2000','2026-01-01','active');
            INSERT INTO asset_depreciations VALUES(1,1,'100',1),(2,2,'200',2);
            INSERT INTO journal_entries VALUES(1,'2026-12-31','posted'),(2,'2026-12-31','posted');
            INSERT INTO journal_lines VALUES
              (1,1,10,'1000','0'),(2,1,11,'0','100'),
              (3,2,10,'2000','0'),(4,2,12,'0','200');
        """)
        rows=fixed_assets.build_asset_reconciliation_rows(conn,"2026-12-31")
        conn.close()
        self.assertEqual(len(rows),3)
        self.assertTrue(all(row["difference"]==Decimal("0") for row in rows))
        cost=next(row for row in rows if row["kind"]=="تكلفة الأصول")
        self.assertEqual(cost["register"],Decimal("3000"))
        self.assertEqual(cost["gl"],Decimal("3000"))



class ManualAssetRouteIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE accounts(
                id INTEGER PRIMARY KEY, code TEXT, name_ar TEXT,
                account_type TEXT, is_group INTEGER DEFAULT 0, active INTEGER DEFAULT 1
            );
            INSERT INTO accounts VALUES
                (10,'1500','تكلفة الأصول','asset',0,1),
                (11,'1591','مجمع إهلاك أصل','asset',0,1),
                (12,'6100','مصروف الإهلاك','expense',0,1),
                (20,'1100','البنك','asset',0,1);
            CREATE TABLE journal_entries(
                id INTEGER PRIMARY KEY, entry_date TEXT, reference TEXT UNIQUE,
                description TEXT, status TEXT, created_by INTEGER, created_at TEXT
            );
            CREATE TABLE journal_lines(
                id INTEGER PRIMARY KEY, entry_id INTEGER, account_id INTEGER,
                debit TEXT, credit TEXT, memo TEXT
            );
            CREATE TABLE fixed_assets(
                id INTEGER PRIMARY KEY, asset_code TEXT UNIQUE, asset_name TEXT,
                category TEXT, purchase_date TEXT, ready_date TEXT, cost TEXT,
                residual_value TEXT, useful_life_months INTEGER,
                depreciation_method TEXT, annual_rate TEXT, total_units TEXT,
                asset_account_id INTEGER, accum_account_id INTEGER,
                expense_account_id INTEGER, notes TEXT, created_by INTEGER,
                status TEXT DEFAULT 'active'
            );
            CREATE TABLE fixed_asset_journal_lines(
                journal_line_id INTEGER PRIMARY KEY, entry_id INTEGER,
                asset_name TEXT, category TEXT, ready_date TEXT, cost TEXT,
                status TEXT, fixed_asset_id INTEGER UNIQUE
            );
        """)
        self.app = Flask(__name__)
        self.app.secret_key = "test-only"
        self.app.add_url_rule("/login", "login", lambda: "login")
        self.app.add_url_rule("/dashboard", "dashboard", lambda: "dashboard")
        self.app.register_blueprint(fixed_assets.assets_bp)
        self.client = self.app.test_client()
        self.db_patch = patch("fixed_assets.db", return_value=self.conn)
        self.code_patch = patch("fixed_assets.next_code", return_value="AST-TEST-001")
        self.db_patch.start()
        self.code_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.code_patch.stop)
        self.addCleanup(self.conn.close)
        with self.client.session_transaction() as sess:
            sess["user_id"] = 7
            sess["role"] = "accountant"

    def _payload(self):
        return {
            "asset_name": "سيارة نقل", "category": "سيارات",
            "purchase_date": "2026-11-01", "ready_date": "2026-11-01",
            "cost": "25000", "residual_value": "1000",
            "useful_life_months": "48", "depreciation_method": "straight_line",
            "annual_rate": "0", "total_units": "0",
            "asset_account_id": "10", "accum_account_id": "11",
            "expense_account_id": "12", "counterpart_account_id": "20",
            "notes": "اختبار تكامل",
        }

    def test_manual_asset_post_creates_asset_balanced_journal_and_link(self):
        response = self.client.post("/assets/", data=self._payload())
        self.assertEqual(response.status_code, 302)
        asset = self.conn.execute("SELECT * FROM fixed_assets").fetchone()
        self.assertIsNotNone(asset)
        entry = self.conn.execute(
            "SELECT * FROM journal_entries WHERE reference='ASSET-ADD-AST-TEST-001'"
        ).fetchone()
        self.assertIsNotNone(entry)
        lines = self.conn.execute(
            "SELECT id,account_id,debit,credit FROM journal_lines WHERE entry_id=? ORDER BY id",
            (entry["id"],),
        ).fetchall()
        self.assertEqual(len(lines), 2)
        from decimal import Decimal
        debit = sum((Decimal(row["debit"]) for row in lines), Decimal("0"))
        credit = sum((Decimal(row["credit"]) for row in lines), Decimal("0"))
        self.assertEqual((debit, credit), (Decimal("25000.00"), Decimal("25000.00")))
        link = self.conn.execute(
            "SELECT * FROM fixed_asset_journal_lines WHERE fixed_asset_id=?",
            (asset["id"],),
        ).fetchone()
        self.assertIsNotNone(link)
        self.assertEqual(link["journal_line_id"], lines[0]["id"])
        self.assertEqual(link["entry_id"], entry["id"])
        self.assertEqual(link["status"], "configured")

    def test_manual_asset_link_failure_rolls_back_asset_and_journal(self):
        self.conn.execute("""
            CREATE TRIGGER fail_asset_link BEFORE INSERT ON fixed_asset_journal_lines
            BEGIN SELECT RAISE(ABORT,'forced link failure'); END
        """)
        response = self.client.post("/assets/", data=self._payload())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM fixed_assets").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM journal_lines").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
