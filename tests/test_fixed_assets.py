        conn.close()
        self.assertEqual(len(rows),3)
        self.assertTrue(all(row["difference"]==Decimal("0") for row in rows))
        cost=next(row for row in rows if row["kind"]=="تكلفة الأصول")
        self.assertEqual(cost["register"],Decimal("3000"))
        self.assertEqual(cost["gl"],Decimal("3000"))



class _DecimalSum:
    def __init__(self):
        self.total = Decimal("0")

    def step(self, value):
        if value is not None:
            self.total += Decimal(str(value))

    def finalize(self):
        return str(self.total)


class ManualAssetRouteIntegrationTests(unittest.TestCase):
    def setUp(self):
        # Keep an anchor connection open while each request receives its own
        # connection; the production route closes its connection before rendering.
        self.db_uri = f"file:fixed_asset_test_{id(self)}?mode=memory&cache=shared"
        self.conn = sqlite3.connect(self.db_uri, uri=True, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.create_aggregate("DECIMAL_SUM", 1, _DecimalSum)
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
                status TEXT DEFAULT 'active', supplier_bill_id INTEGER
            );
            CREATE TABLE fixed_asset_journal_lines(
                journal_line_id INTEGER PRIMARY KEY, entry_id INTEGER,
                asset_name TEXT, category TEXT, ready_date TEXT, cost TEXT,
                status TEXT, fixed_asset_id INTEGER UNIQUE
            );
            CREATE TABLE fixed_asset_supplier_bill_links(
                bill_id INTEGER, asset_id INTEGER UNIQUE
            );
            CREATE TABLE supplier_bills(
                id INTEGER PRIMARY KEY, bill_no TEXT, supplier_invoice_no TEXT
            );
            CREATE TABLE asset_depreciations(
                id INTEGER PRIMARY KEY, asset_id INTEGER, amount TEXT
            );
        """)
        self.app = Flask(__name__)
        self.app.secret_key = "test-only"
        # The route re-renders this page after a posting error; isolate this test
        # from the full application layout and its unrelated endpoint dependencies.
        self.app.jinja_loader = DictLoader({"fixed_assets.html": "asset form"})
        self.app.add_url_rule("/login", "login", lambda: "login")
        self.app.add_url_rule("/dashboard", "dashboard", lambda: "dashboard")
        self.app.register_blueprint(fixed_assets.assets_bp)
        self.client = self.app.test_client()
        self.db_patch = patch("fixed_assets.db", side_effect=self._connect)
        self.code_patch = patch("fixed_assets.next_code", return_value="AST-TEST-001")
        self.db_patch.start()
        self.code_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.code_patch.stop)
        self.addCleanup(self.conn.close)
        with self.client.session_transaction() as sess:
            sess["user_id"] = 7
            sess["role"] = "accountant"

    def _connect(self):
        conn = sqlite3.connect(self.db_uri, uri=True, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.create_aggregate("DECIMAL_SUM", 1, _DecimalSum)
        return conn

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
        self.conn.commit()
        response = self.client.post("/assets/", data=self._payload())
        # The route catches the database error, rolls back, and re-renders the form with an error flash.
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM fixed_assets").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM journal_lines").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()