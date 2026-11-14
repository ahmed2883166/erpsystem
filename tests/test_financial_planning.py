import sqlite3
from datetime import date
import unittest
from unittest.mock import patch
from decimal import Decimal

from chart_templates import CHARTS, seed_activity_chart, ensure_activity_chart_accounts
from financial_planning import (initialize_planning_schema, build_balance_sheet_snapshot, _money, _connect, create_financial_year, list_financial_years, create_opening_entry, close_financial_year, reverse_financial_year_entry, build_financial_year_preclose_report, build_budget_comparison, build_budget_monthly_comparison, _budget_months, _split_monthly_amount)
from accounting_core import register_decimal_aggregates, post_journal


class FinancialPlanningTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""CREATE TABLE accounts(
            id INTEGER PRIMARY KEY, code TEXT UNIQUE, name_ar TEXT,
            account_type TEXT, parent_id INTEGER, is_group INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1)""")
        self.conn.executescript("""
            CREATE TABLE journal_entries(id INTEGER PRIMARY KEY,entry_date TEXT,status TEXT);
            CREATE TABLE journal_lines(id INTEGER PRIMARY KEY,entry_id INTEGER,account_id INTEGER,debit TEXT,credit TEXT,
                branch_id INTEGER,cost_center_id INTEGER,project_id INTEGER);
        """)
        register_decimal_aggregates(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_budget_pdf_export_returns_pdf_file(self):
        from flask import Flask
        import financial_planning

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE company_settings(
            setting_key TEXT PRIMARY KEY, setting_value TEXT
        )""")
        conn.executemany("INSERT INTO company_settings VALUES(?,?)", [
            ("company_name", "NEXUS Test"), ("base_currency", "EGP")
        ])
        plan = {
            "id": 1, "name": "موازنة اختبار",
            "date_from": "2026-01-01", "date_to": "2026-01-31"
        }
        app = Flask(__name__)
        app.secret_key = "test-secret"
        app.register_blueprint(financial_planning.planning_bp)

        with (
            patch("financial_planning._connect", return_value=conn),
            patch("financial_planning.build_budget_monthly_comparison",
                  return_value=(plan, [])),
        ):
            client = app.test_client()
            with client.session_transaction() as session:
                session["user_id"] = 1
                session["permissions"] = ["budget.view"]
            response = client.get("/budget/1/export/pdf")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertTrue(response.data.startswith(b"%PDF-"))

    def test_connect_uses_centralized_database_path(self):
        with patch("financial_planning.DB_PATH", "/tmp/nexus-test.db"), \
             patch("financial_planning.connect_database", return_value="connection") as connect:
            self.assertEqual(_connect(), "connection")
            connect.assert_called_once_with("/tmp/nexus-test.db")

    def test_financial_year_creation_and_overlap_protection(self):
        initialize_planning_schema(self.conn)
        year_id = create_financial_year(self.conn, "2026", "2026-01-01", "2026-12-31", 1)
        self.assertGreater(year_id, 0)
        years = list_financial_years(self.conn)
        self.assertEqual(len(years), 1)
        self.assertEqual(years[0]["status"], "open")
        with self.assertRaisesRegex(ValueError, "تتداخل"):
            create_financial_year(self.conn, "FY2026-27", "2026-12-01", "2027-11-30", 1)
        with self.assertRaises(ValueError):
            create_financial_year(self.conn, "Bad dates", "2027-02-01", "2027-01-01", 1)
        self.assertEqual(len(list_financial_years(self.conn)), 1)

    def test_financial_year_overlap_does_not_require_sqlite_row_factory(self):
        conn = sqlite3.connect(":memory:")
        try:
            initialize_planning_schema(conn)
            create_financial_year(conn, "FY 2026", "2026-01-01", "2026-12-31")
            with self.assertRaisesRegex(ValueError, "FY 2026"):
                create_financial_year(conn, "FY 2027", "2026-12-15", "2027-12-14")
        finally:
            conn.close()

    def test_budget_schema_and_decimal_amounts(self):
        initialize_planning_schema(self.conn)
        tables = {r["name"] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("budget_plans", tables)
        self.assertIn("budget_lines", tables)
        budget_columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(budget_plans)")}
        self.assertTrue({"branch_id", "cost_center_id", "project_id"}.issubset(budget_columns))
        self.assertEqual(_money("123.456"), Decimal("123.46"))
        with self.assertRaises(ValueError):
            _money("-1")
        with self.assertRaises(Exception):
            _money("NaN")


    def test_budget_comparison_filters_dimensions_and_flags_80_percent(self):
        initialize_planning_schema(self.conn)
        self.conn.executemany(
            "INSERT INTO accounts(id,code,name_ar,account_type,is_group,active) VALUES(?,?,?,?,0,1)",
            [(1, "6100", "مصروفات تشغيل", "expense"), (2, "4100", "إيرادات خدمات", "revenue")]
        )
        self.conn.execute("""INSERT INTO budget_plans
            (id,name,date_from,date_to,branch_id,cost_center_id)
            VALUES(1,'موازنة فرع','2026-11-01','2026-11-30',1,2)""")
        self.conn.executemany(
            "INSERT INTO budget_lines(budget_id,account_id,amount) VALUES(1,?,?)",
            [(1, "1000.00"), (2, "500.00")]
        )
        self.conn.executemany(
            "INSERT INTO journal_entries(id,entry_date,status) VALUES(?,?,?)",
            [(1, "2026-11-10", "posted"), (2, "2026-11-11", "posted"),
             (3, "2026-11-12", "draft"), (4, "2026-11-13", "posted")]
        )
        self.conn.executemany(
            """INSERT INTO journal_lines(entry_id,account_id,debit,credit,branch_id,cost_center_id)
               VALUES(?,?,?,?,?,?)""",
            [(1, 1, "850", "0", 1, 2), (2, 1, "400", "0", 2, 2),
             (3, 1, "999", "0", 1, 2), (4, 2, "0", "600", 1, 2)]
        )
        plan, rows, totals = build_budget_comparison(self.conn, 1)
        by_code = {row["code"]: row for row in rows}
        self.assertEqual(by_code["6100"]["actual"], Decimal("850"))
        self.assertEqual(by_code["6100"]["status"], "warning")
        self.assertEqual(by_code["4100"]["actual"], Decimal("600"))
        self.assertEqual(by_code["4100"]["status"], "ahead")
        self.assertEqual(totals["expense_actual"], Decimal("850"))
        self.assertEqual(totals["revenue_actual"], Decimal("600"))

    def test_monthly_budget_split_preserves_exact_total_and_remainder(self):
        months = _budget_months("2026-01-15", "2026-03-20")
        self.assertEqual(months, ["2026-01", "2026-02", "2026-03"])
        values = _split_monthly_amount(Decimal("100.00"), months)
        self.assertEqual(values, [Decimal("33.33"), Decimal("33.33"), Decimal("33.34")])
        self.assertEqual(sum(values), Decimal("100.00"))

    def test_monthly_budget_actual_comparison_uses_posted_lines_and_exact_decimals(self):
        initialize_planning_schema(self.conn)
        self.conn.executemany(
            "INSERT INTO accounts(id,code,name_ar,account_type,is_group,active) VALUES(?,?,?,?,0,1)",
            [(1, "6100", "مصروف تشغيل", "expense")]
        )
        self.conn.execute("""INSERT INTO budget_plans
            (id,name,date_from,date_to,branch_id) VALUES(1,'موازنة','2026-01-01','2026-02-28',1)""")
        self.conn.execute("INSERT INTO budget_lines(budget_id,account_id,amount) VALUES(1,1,'100.00')")
        self.conn.executemany(
            "INSERT INTO budget_monthly_lines(budget_id,account_id,period,amount) VALUES(1,1,?,?)",
            [("2026-01","40.00"),("2026-02","60.00")]
        )
        self.conn.executemany(
            "INSERT INTO journal_entries(id,entry_date,status) VALUES(?,?,?)",
            [(1,"2026-01-05","posted"),(2,"2026-01-20","posted"),(3,"2026-01-22","draft"),(4,"2026-02-02","posted")]
        )
        self.conn.executemany(
            "INSERT INTO journal_lines(entry_id,account_id,debit,credit,branch_id) VALUES(?,?,?,?,?)",
            [(1,1,"10.10","0",1),(2,1,"20.20","0",1),(3,1,"999","0",1),(4,1,"60.00","0",1)]
        )
        _, rows = build_budget_monthly_comparison(self.conn, 1)
        by_period = {r["period"]: r for r in rows}
        self.assertEqual(by_period["2026-01"]["actual"], Decimal("30.30"))
        self.assertEqual(by_period["2026-02"]["actual"], Decimal("60.00"))
        self.assertEqual(by_period["2026-01"]["variance"], Decimal("-9.70"))

    def test_budget_audit_history_is_append_only(self):
        initialize_planning_schema(self.conn)
        self.conn.execute("INSERT INTO budget_plans(id,name,date_from,date_to) VALUES(1,'X','2026-01-01','2026-12-31')")
        self.conn.execute("INSERT INTO budget_audit_log(budget_id,action,actor_id) VALUES(1,'created',1)")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.conn.execute("UPDATE budget_audit_log SET action='changed' WHERE budget_id=1")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.conn.execute("DELETE FROM budget_audit_log WHERE budget_id=1")

    def test_budget_comparison_rejects_missing_budget(self):
        initialize_planning_schema(self.conn)
        with self.assertRaisesRegex(ValueError, "غير موجودة"):
            build_budget_comparison(self.conn, 999)

    def test_balance_sheet_includes_profit_and_excludes_drafts(self):
        self.conn.executemany("INSERT INTO accounts(id,code,name_ar,account_type,is_group) VALUES(?,?,?,?,0)", [
            (1,"1110","الصندوق","asset"),(2,"3100","رأس المال","equity"),
            (3,"4100","الإيرادات","revenue"),(4,"6100","المصروفات","expense")])
        entries=[
            (1,"2026-01-01","posted",[(1,"1000","0"),(2,"0","1000")]),
            (2,"2026-01-02","posted",[(1,"200","0"),(3,"0","200")]),
            (3,"2026-01-03","posted",[(4,"50","0"),(1,"0","50")]),
            (4,"2026-01-04","draft",[(1,"999","0"),(3,"0","999")]),
        ]
        for entry_id,entry_date,status,lines in entries:
            self.conn.execute("INSERT INTO journal_entries(id,entry_date,status) VALUES(?,?,?)",(entry_id,entry_date,status))
            for account_id,debit,credit in lines:
                self.conn.execute("INSERT INTO journal_lines(entry_id,account_id,debit,credit) VALUES(?,?,?,?)",
                                  (entry_id,account_id,debit,credit))
        s=build_balance_sheet_snapshot(self.conn,"2026-01-31")
        self.assertEqual(s["total_assets"],Decimal("1150"))
        self.assertEqual(s["total_liabilities"],Decimal("0"))
        self.assertEqual(s["net_profit"],Decimal("150"))
        self.assertEqual(s["total_equity"],Decimal("1150"))
        self.assertTrue(s["balanced"])

    def test_each_activity_chart_has_unique_account_codes(self):
        for activity, rows in CHARTS.items():
            codes = [row[0] for row in rows]
            self.assertEqual(len(codes), len(set(codes)), activity)

    def test_new_commercial_chart_is_hierarchical(self):
        seed_activity_chart(self.conn, "commercial")
        fixed = self.conn.execute("SELECT id,is_group FROM accounts WHERE code='1240'").fetchone()
        child = self.conn.execute("SELECT parent_id FROM accounts WHERE code='1241'").fetchone()
        wages = self.conn.execute("SELECT id,is_group FROM accounts WHERE code='6600'").fetchone()
        marketing = self.conn.execute("SELECT id,is_group FROM accounts WHERE code='6700'").fetchone()
        self.assertEqual(fixed["is_group"], 1)
        self.assertEqual(child["parent_id"], fixed["id"])
        self.assertEqual(wages["is_group"], 1)
        self.assertEqual(marketing["is_group"], 1)

    def test_legacy_chart_gets_common_branches_without_rewriting_accounts(self):
        self.conn.execute("INSERT INTO accounts(code,name_ar,account_type,is_group) VALUES('1000','أصل قديم','asset',0)")
        original = self.conn.execute("SELECT id,name_ar FROM accounts WHERE code='1000'").fetchone()
        ensure_activity_chart_accounts(self.conn, "commercial")
        after = self.conn.execute("SELECT id,name_ar FROM accounts WHERE code='1000'").fetchone()
        self.assertEqual(tuple(original), tuple(after))
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM accounts WHERE code='1240' AND is_group=1").fetchone())
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM accounts WHERE code='6610'").fetchone())
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM accounts WHERE code='1210' AND is_group=1").fetchone())
        fixed_child = self.conn.execute("SELECT parent_id FROM accounts WHERE code='1211'").fetchone()
        fixed_group = self.conn.execute("SELECT id FROM accounts WHERE code='1210'").fetchone()
        self.assertEqual(fixed_child["parent_id"], fixed_group["id"])




class FinancialYearJournalTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        register_decimal_aggregates(self.conn)
        self.conn.executescript("""
        CREATE TABLE accounts(
            id INTEGER PRIMARY KEY,code TEXT UNIQUE,name_ar TEXT,account_type TEXT,
            parent_id INTEGER,is_group INTEGER DEFAULT 0,active INTEGER DEFAULT 1);
        CREATE TABLE journal_entries(
            id INTEGER PRIMARY KEY,entry_date TEXT NOT NULL,reference TEXT,
            description TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'posted',
            created_by INTEGER,created_at TEXT NOT NULL);
        CREATE TABLE journal_lines(
            id INTEGER PRIMARY KEY,entry_id INTEGER NOT NULL,account_id INTEGER NOT NULL,
            debit TEXT NOT NULL DEFAULT '0',credit TEXT NOT NULL DEFAULT '0',memo TEXT);
        CREATE TABLE accounting_periods(
            id INTEGER PRIMARY KEY,period_name TEXT,start_date TEXT,end_date TEXT,
            locked INTEGER DEFAULT 0,created_at TEXT);
        INSERT INTO accounts VALUES(1,'1110','الصندوق','asset',NULL,0,1);
        INSERT INTO accounts VALUES(2,'2110','الموردون','liability',NULL,0,1);
        INSERT INTO accounts VALUES(3,'3100','رأس المال','equity',NULL,0,1);
        INSERT INTO accounts VALUES(4,'3200','الأرباح المرحلة','equity',NULL,0,1);
        INSERT INTO accounts VALUES(5,'4100','إيرادات المبيعات','revenue',NULL,0,1);
        INSERT INTO accounts VALUES(6,'6100','مصروفات التشغيل','expense',NULL,0,1);
        """)
        initialize_planning_schema(self.conn)
        self.year_id = create_financial_year(
            self.conn, "2026", "2026-01-01", "2026-12-31", 1
        )

    def tearDown(self):
        self.conn.close()

    def _opening(self):
        return create_opening_entry(self.conn, self.year_id,
            {"1": "1000", "2": "200", "3": "800", "4": "0"}, 1)

    def _activity(self):
        post_journal(self.conn, "2026-03-01", "SALE-2026-1", "بيع",
            [(1, "300", "0", "تحصيل نقدي"), (5, "0", "300", "إيراد")], 1)
        self.conn.commit()
        post_journal(self.conn, "2026-04-01", "EXP-2026-1", "مصروف",
            [(6, "100", "0", "مصروف تشغيل"), (1, "0", "100", "سداد")], 1)
        self.conn.commit()

    def _create_closed_previous_year(self):
        previous_id = create_financial_year(
            self.conn, "2025", "2025-01-01", "2025-12-31", 1
        )
        create_opening_entry(
            self.conn, previous_id,
            {"1": "1000", "2": "200", "3": "800", "4": "0"}, 1
        )
        post_journal(self.conn, "2025-03-01", "SALE-2025-1", "بيع سابق",
            [(1, "300", "0", "تحصيل"), (5, "0", "300", "إيراد")], 1)
        self.conn.commit()
        post_journal(self.conn, "2025-04-01", "EXP-2025-1", "مصروف سابق",
            [(6, "100", "0", "مصروف"), (1, "0", "100", "سداد")], 1)
        self.conn.commit()
        close_financial_year(self.conn, previous_id, 1)
        return previous_id

    def test_opening_rejects_when_posted_entries_already_exist_in_year(self):
        post_journal(self.conn, "2026-02-01", "EARLY-POST", "قيد سابق للافتتاح",
            [(1, "50", "0", "مدين"), (2, "0", "50", "دائن")], 1)
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, "قيود مرحلة داخل السنة"):
            self._opening()
        self.assertIsNone(self.conn.execute(
            "SELECT opening_entry_id FROM financial_years WHERE id=?", (self.year_id,)
        ).fetchone()[0])

    def test_opening_must_match_previous_closed_year_balances(self):
        self._create_closed_previous_year()
        with self.assertRaisesRegex(ValueError, "لا تطابق الأرصدة الدفترية"):
            create_opening_entry(self.conn, self.year_id,
                {"1": "1199", "2": "200", "3": "800", "4": "199"}, 1)
        self.assertIsNone(self.conn.execute(
            "SELECT opening_entry_id FROM financial_years WHERE id=?", (self.year_id,)
        ).fetchone()[0])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM journal_entries WHERE reference LIKE 'FY-%-OPEN-%'"
        ).fetchone()[0], 1)

    def test_opening_accepts_balances_matching_previous_closed_year(self):
        self._create_closed_previous_year()
        entry_id = create_opening_entry(self.conn, self.year_id,
            {"1": "1200", "2": "200", "3": "800", "4": "200"}, 1)
        self.assertIsNotNone(entry_id)
 
    def test_opening_is_reconciled_even_when_previous_year_is_not_closed(self):
        previous_id = create_financial_year(
            self.conn, "2025", "2025-01-01", "2025-12-31", 1
        )
        create_opening_entry(self.conn, previous_id,
            {"1": "1000", "2": "200", "3": "800", "4": "0"}, 1)
        post_journal(self.conn, "2025-03-01", "SALE-OPEN-2025", "بيع سابق",
            [(1, "300", "0", "تحصيل"), (5, "0", "300", "إيراد")], 1)
        self.conn.commit()
        post_journal(self.conn, "2025-04-01", "EXP-OPEN-2025", "مصروف سابق",
            [(6, "100", "0", "مصروف"), (1, "0", "100", "سداد")], 1)
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, "لا تطابق الأرصدة الدفترية"):
            create_opening_entry(self.conn, self.year_id,
                {"1": "1199", "2": "200", "3": "800", "4": "199"}, 1)
        entry_id = create_opening_entry(self.conn, self.year_id,
            {"1": "1200", "2": "200", "3": "800", "4": "200"}, 1)
        self.assertIsNotNone(entry_id)

    def test_opening_reconciles_legacy_balances_without_previous_year_record(self):
        post_journal(self.conn, "2025-01-01", "LEGACY-OPEN", "أرصدة تاريخية",
            [(1, "1000", "0", "صندوق"), (2, "0", "200", "موردون"),
             (3, "0", "800", "رأس مال")], 1)
        self.conn.commit()
        post_journal(self.conn, "2025-03-01", "LEGACY-SALE", "بيع تاريخي",
            [(1, "300", "0", "تحصيل"), (5, "0", "300", "إيراد")], 1)
        self.conn.commit()
        post_journal(self.conn, "2025-04-01", "LEGACY-EXP", "مصروف تاريخي",
            [(6, "100", "0", "مصروف"), (1, "0", "100", "سداد")], 1)
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, "أرصدة الشركة التاريخية"):
            create_opening_entry(self.conn, self.year_id,
                {"1": "1100", "2": "200", "3": "800", "4": "100"}, 1)
        entry_id = create_opening_entry(self.conn, self.year_id,
            {"1": "1200", "2": "200", "3": "800", "4": "200"}, 1)
        self.assertIsNotNone(entry_id)

    def test_opening_entry_balances_and_links_once(self):
        entry_id = self._opening()
        self.assertEqual(self.conn.execute(
            "SELECT opening_entry_id FROM financial_years WHERE id=?", (self.year_id,)
        ).fetchone()[0], entry_id)
        totals = self.conn.execute(
            "SELECT DECIMAL_SUM(debit),DECIMAL_SUM(credit) FROM journal_lines WHERE entry_id=?",
            (entry_id,)
        ).fetchone()
        self.assertEqual(Decimal(totals[0]), Decimal("1000.00"))
        self.assertEqual(Decimal(totals[1]), Decimal("1000.00"))
        with self.assertRaisesRegex(ValueError, "بالفعل"):
            self._opening()

    def test_opening_accepts_debit_balances_in_liability_and_equity_accounts(self):
        entry_id = create_opening_entry(
            self.conn, self.year_id,
            {"1": "1000", "2": "-100", "3": "1100", "4": "0"}, 1
        )
        lines = {
            row["account_id"]: (Decimal(row["debit"]), Decimal(row["credit"]))
            for row in self.conn.execute(
                "SELECT account_id,debit,credit FROM journal_lines WHERE entry_id=?",
                (entry_id,),
            )
        }
        self.assertEqual(lines[1], (Decimal("1000.00"), Decimal("0.00")))
        self.assertEqual(lines[2], (Decimal("100.00"), Decimal("0.00")))
        self.assertEqual(lines[3], (Decimal("0.00"), Decimal("1100.00")))

    def test_unbalanced_opening_is_rejected_without_journal(self):
        with self.assertRaisesRegex(ValueError, "غير متوازن"):
            create_opening_entry(self.conn, self.year_id,
                {"1": "1000", "2": "200", "3": "700"}, 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0], 0)
        self.assertIsNone(self.conn.execute(
            "SELECT opening_entry_id FROM financial_years WHERE id=?", (self.year_id,)
        ).fetchone()[0])

    def test_real_financial_year_close_keeps_dashboard_profit_and_next_year_opening_equity(self):
        from management_dashboard import (
            initialize_management_dashboard_schema,
            cached_management_snapshot,
            dashboard_period_balances,
        )
        self.conn.execute("""CREATE TABLE company_settings(
            setting_key TEXT PRIMARY KEY,setting_value TEXT NOT NULL
        )""")
        self.conn.executemany("INSERT INTO company_settings VALUES(?,?)", [
            ("activity", "commercial"), ("base_currency", "EGP"),
            ("company_name", "NEXUS test"),
        ])
        initialize_management_dashboard_schema(self.conn, "commercial")
        self._opening()
        self._activity()
        before_close = cached_management_snapshot(
            self.conn, date(2026,1,1), date(2026,12,31)
        )
        self.assertEqual(before_close["current"]["net_profit"], Decimal("200.00"))
        self.assertEqual(before_close["total_equity"], Decimal("800.00"))

        closing_id = close_financial_year(
            self.conn, self.year_id, 1, allow_warnings=True
        )
        closing = self.conn.execute(
            "SELECT entry_type,status FROM journal_entries WHERE id=?",
            (closing_id,),
        ).fetchone()
        self.assertEqual(tuple(closing), ("year_end_closing", "posted"))
        year = self.conn.execute(
            "SELECT status,closing_entry_id FROM financial_years WHERE id=?",
            (self.year_id,),
        ).fetchone()
        self.assertEqual(tuple(year), ("closed", closing_id))

        snapshot = cached_management_snapshot(
            self.conn, date(2026,1,1), date(2026,12,31)
        )
        self.assertEqual(snapshot["current"]["revenue"], Decimal("300.00"))
        self.assertEqual(snapshot["current"]["expenses"], Decimal("100.00"))
        self.assertEqual(snapshot["current"]["net_profit"], Decimal("200.00"))
        self.assertEqual(snapshot["cash"], Decimal("1200.00"))
        self.assertEqual(snapshot["total_equity"], Decimal("1000.00"))
        self.assertTrue(snapshot["balance_ok"])

        _current, _previous, _as_of, next_year_opening = dashboard_period_balances(
            self.conn, "2027-01-01", "2027-01-31",
            "2026-12-01", "2026-12-31", exclude_closing_entries=True,
        )
        self.assertEqual(next_year_opening[4]["credit"], Decimal("200.00"))

    def test_close_resolves_retained_earnings_from_activity_role(self):
        self.conn.execute("""INSERT INTO accounts
            (id,code,name_ar,account_type,parent_id,is_group,active)
            VALUES(7,'3990','أرباح مرحلة مخصصة','equity',NULL,0,1)""")
        self.conn.commit()
        self._opening()
        self._activity()
        with patch("financial_planning.ACTIVITY_ACCOUNT_ROLES", {
            "commercial": {"retained_earnings": ("3990",)}
        }):
            closing_id = close_financial_year(self.conn, self.year_id, 1)
        self.assertIsNotNone(self.conn.execute("""SELECT 1 FROM journal_lines
            WHERE entry_id=? AND account_id=7 AND credit='200.00'""",
            (closing_id,)).fetchone())

    def test_preclose_report_flags_drafts_as_blockers_without_mutating_data(self):
        self.conn.execute("""INSERT INTO journal_entries
            (entry_date,reference,description,status,created_by,created_at)
            VALUES('2026-06-01','DRAFT-PRE','مسودة','draft',1,'now')""")
        self.conn.commit()
        before = self.conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0]
        report = build_financial_year_preclose_report(self.conn, self.year_id)
        after = self.conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0]
        self.assertFalse(report["can_close"])
        self.assertEqual(report["blockers"][0]["code"], "draft_entries")
        self.assertEqual(before, after)

    def test_preclose_report_detects_unbalanced_posted_journal(self):
        self.conn.execute("""INSERT INTO journal_entries
            (entry_date,reference,description,status,created_by,created_at)
            VALUES('2026-06-01','BAD-POST','قيد غير متوازن','posted',1,'now')""")
        entry_id = self.conn.execute("SELECT id FROM journal_entries WHERE reference='BAD-POST'").fetchone()[0]
        self.conn.execute("""INSERT INTO journal_lines(entry_id,account_id,debit,credit)
            VALUES(?,?,?,?)""", (entry_id, 1, "100.00", "0"))
        self.conn.commit()
        report = build_financial_year_preclose_report(self.conn, self.year_id)
        self.assertTrue(any(item["code"] == "unbalanced_entries" for item in report["blockers"]))

    def test_preclose_report_warns_when_opening_entry_is_missing(self):
        report = build_financial_year_preclose_report(self.conn, self.year_id)
        self.assertTrue(report["can_close"])
        self.assertTrue(any(item["code"] == "opening_entry" for item in report["warnings"]))

    def test_close_requires_admin_approval_for_warnings(self):
        with self.assertRaisesRegex(ValueError, "تحذيرات"):
            close_financial_year(self.conn, self.year_id, 1)
        self.assertEqual(
            self.conn.execute("SELECT status FROM financial_years WHERE id=?", (self.year_id,)).fetchone()[0],
            "open",
        )

    def test_close_rejects_drafts(self):
        self.conn.execute("""INSERT INTO journal_entries
            (entry_date,reference,description,status,created_by,created_at)
            VALUES('2026-06-01','DRAFT-1','مسودة','draft',1,'now')""")
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, "مسودة"):
            close_financial_year(self.conn, self.year_id, 1)
        year = self.conn.execute("SELECT status,closing_entry_id FROM financial_years WHERE id=?",
                                 (self.year_id,)).fetchone()
        self.assertEqual(year["status"], "open")
        self.assertIsNone(year["closing_entry_id"])

    def test_close_zeroes_revenue_and_expenses_and_trial_balance_balances(self):
        self._opening()
        self._activity()
        closing_id = close_financial_year(self.conn, self.year_id, 1)
        year = self.conn.execute("SELECT status,closing_entry_id FROM financial_years WHERE id=?",
                                 (self.year_id,)).fetchone()
        self.assertEqual(year["status"], "closed")
        self.assertEqual(year["closing_entry_id"], closing_id)
        self.assertEqual(
            self.conn.execute("SELECT entry_type FROM journal_entries WHERE id=?", (closing_id,)).fetchone()[0],
            "year_end_closing",
        )
        for account_id in (5, 6):
            row = self.conn.execute("""SELECT DECIMAL_SUM(jl.debit),DECIMAL_SUM(jl.credit)
                FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
                WHERE jl.account_id=? AND je.status='posted'""", (account_id,)).fetchone()
            self.assertEqual(Decimal(row[0] or "0") - Decimal(row[1] or "0"), Decimal("0.00"))
        retained = self.conn.execute("""SELECT DECIMAL_SUM(debit),DECIMAL_SUM(credit)
            FROM journal_lines WHERE account_id=4""").fetchone()
        self.assertEqual(Decimal(retained[1]) - Decimal(retained[0]), Decimal("200.00"))
        totals = self.conn.execute("""SELECT DECIMAL_SUM(jl.debit),DECIMAL_SUM(jl.credit)
            FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
            WHERE je.status='posted'""").fetchone()
        self.assertEqual(Decimal(totals[0]), Decimal(totals[1]))
        snapshot = build_balance_sheet_snapshot(self.conn, "2026-12-31")
        self.assertTrue(snapshot["balanced"])
        with self.assertRaisesRegex(ValueError, "ليست مفتوحة|بالفعل"):
            close_financial_year(self.conn, self.year_id, 1)

    def test_locked_year_end_period_blocks_close(self):
        self._opening()
        self._activity()
        self.conn.execute("""INSERT INTO accounting_periods
            (period_name,start_date,end_date,locked,created_at)
            VALUES('ديسمبر 2026','2026-12-01','2026-12-31',1,'now')""")
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, "مغلقة بالفعل"):
            close_financial_year(self.conn, self.year_id, 1)

    def test_posting_into_closed_year_is_blocked(self):
        self._opening()
        self._activity()
        close_financial_year(self.conn, self.year_id, 1)
        with self.assertRaisesRegex(ValueError, "ليست مفتوحة"):
            post_journal(self.conn, "2026-11-01", "LATE-POST", "قيد بعد الإقفال",
                [(1, "10", "0", "مدين"), (5, "0", "10", "دائن")], 1)
        self.conn.rollback()

    def test_reversing_opening_keeps_original_and_clears_active_link(self):
        opening_id = self._opening()
        reversal_id = reverse_financial_year_entry(
            self.conn, self.year_id, "opening", "2026-01-01", "تصحيح الأرصدة الافتتاحية", 1
        )
        year = self.conn.execute("SELECT status,opening_entry_id FROM financial_years WHERE id=?",
                                 (self.year_id,)).fetchone()
        self.assertEqual(year["status"], "open")
        self.assertIsNone(year["opening_entry_id"])
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM journal_entries WHERE id=? AND status='posted'", (opening_id,)
        ).fetchone())
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM journal_entries WHERE id=? AND status='posted'", (reversal_id,)
        ).fetchone())
        replacement_id = self._opening()
        self.assertNotEqual(opening_id, replacement_id)

    def test_reversing_close_is_blocked_after_next_year_opening(self):
        self._opening()
        self._activity()
        close_financial_year(self.conn, self.year_id, 1)
        next_year_id = create_financial_year(
            self.conn, "2027", "2027-01-01", "2027-12-31", 1
        )
        create_opening_entry(self.conn, next_year_id,
            {"1": "1200", "2": "200", "3": "800", "4": "200"}, 1)
        with self.assertRaisesRegex(ValueError, "السنة التالية"):
            reverse_financial_year_entry(
                self.conn, self.year_id, "closing", "2026-12-31", "محاولة عكس بعد افتتاح السنة التالية", 1
            )
        self.assertEqual(
            self.conn.execute("SELECT status FROM financial_years WHERE id=?", (self.year_id,)).fetchone()[0],
            "closed",
        )

    def test_reversing_close_is_blocked_by_later_journal_even_without_year_record(self):
        self._opening()
        self._activity()
        close_financial_year(self.conn, self.year_id, 1)
        post_journal(self.conn, "2027-02-01", "NEXT-YEAR-ENTRY", "قيد في السنة التالية",
            [(1, "10", "0", "مدين"), (2, "0", "10", "دائن")], 1)
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, "يوجد قيد رقم"):
            reverse_financial_year_entry(
                self.conn, self.year_id, "closing", "2026-12-31", "عكس بعد قيد لاحق", 1
            )
        self.assertEqual(
            self.conn.execute("SELECT status FROM financial_years WHERE id=?", (self.year_id,)).fetchone()[0],
            "closed",
        )

    def test_reversing_close_reopens_year_and_keeps_original(self):
        self._opening()
        self._activity()
        closing_id = close_financial_year(self.conn, self.year_id, 1)
        reversal_id = reverse_financial_year_entry(
            self.conn, self.year_id, "closing", "2026-12-31", "تصحيح الإقفال", 1
        )
        year = self.conn.execute("SELECT status,closing_entry_id FROM financial_years WHERE id=?",
                                 (self.year_id,)).fetchone()
        self.assertEqual(year["status"], "open")
        self.assertIsNone(year["closing_entry_id"])
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM journal_entries WHERE id=? AND status='posted'", (closing_id,)
        ).fetchone())
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM journal_entries WHERE id=? AND status='posted'", (reversal_id,)
        ).fetchone())


if __name__ == "__main__":
    unittest.main()
