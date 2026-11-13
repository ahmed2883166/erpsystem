import sqlite3
import unittest
from datetime import date
from decimal import Decimal

from management_dashboard import build_management_snapshot, load_management_account_roles, initialize_management_dashboard_schema, cash_forecast, unusual_journal_entries
from chart_templates import ACTIVITY_ACCOUNT_ROLES


class ManagementDashboardTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE accounts(id INTEGER PRIMARY KEY,code TEXT,name_ar TEXT,account_type TEXT,is_group INTEGER DEFAULT 0,active INTEGER DEFAULT 1);
            CREATE TABLE journal_entries(id INTEGER PRIMARY KEY,entry_date TEXT,status TEXT,entry_type TEXT NOT NULL DEFAULT 'general');
            CREATE TABLE journal_lines(id INTEGER PRIMARY KEY,entry_id INTEGER,account_id INTEGER,debit TEXT,credit TEXT);
            CREATE TABLE company_settings(setting_key TEXT PRIMARY KEY,setting_value TEXT);
            INSERT INTO company_settings VALUES('activity','commercial');
            INSERT INTO company_settings VALUES('base_currency','EGP');
            INSERT INTO accounts(id,code,name_ar,account_type,is_group) VALUES
                (1,'1110','الصندوق','asset',0),(2,'1120','البنوك','asset',0),
                (3,'1130','العملاء','asset',0),(4,'1150','المخزون','asset',0),
                (5,'2110','الموردون','liability',0),(6,'3100','رأس المال','equity',0),
                (7,'4100','المبيعات','revenue',0),(8,'5100','تكلفة المبيعات','expense',0),
                (9,'6400','مصروفات إدارية','expense',0);
        """)
        initialize_management_dashboard_schema(self.conn, 'commercial')
        entries = [
            ("2026-10-01","posted",[(1,"1000","0"),(4,"500","0"),(6,"0","1500")]),
            ("2026-10-15","posted",[(3,"200","0"),(7,"0","200")]),
            ("2026-10-15","posted",[(8,"120","0"),(4,"0","120")]),
            ("2026-10-20","posted",[(9,"50","0"),(1,"0","50")]),
            ("2026-11-05","posted",[(1,"500","0"),(7,"0","500")]),
            ("2026-11-05","posted",[(8,"300","0"),(4,"0","300")]),
            ("2026-11-06","posted",[(9,"100","0"),(1,"0","100")]),
            ("2026-11-10","posted",[(4,"100","0"),(5,"0","100")]),
            ("2026-11-12","draft",[(1,"999","0"),(7,"0","999")]),
        ]
        for idx,(entry_date,status,lines) in enumerate(entries,1):
            self.conn.execute("INSERT INTO journal_entries(id,entry_date,status) VALUES(?,?,?)",(idx,entry_date,status))
            for account_id,debit,credit in lines:
                self.conn.execute("INSERT INTO journal_lines(entry_id,account_id,debit,credit) VALUES(?,?,?,?)",
                                  (idx,account_id,debit,credit))

    def tearDown(self):
        self.conn.close()

    def test_period_results_and_comparison(self):
        s=build_management_snapshot(self.conn,date(2026,11,1),date(2026,11,30))
        self.assertEqual(s["current"]["revenue"],Decimal("500"))
        self.assertEqual(s["current"]["expenses"],Decimal("400"))
        self.assertEqual(s["current"]["net_profit"],Decimal("100"))
        self.assertEqual(s["previous"]["revenue"],Decimal("200"))
        self.assertEqual(s["previous"]["net_profit"],Decimal("30"))
        self.assertEqual(s["changes"]["revenue"],Decimal("150.00"))

    def test_dashboard_current_previous_and_asof_balances_use_one_grouped_ledger_scan(self):
        statements = []
        self.conn.set_trace_callback(statements.append)
        snapshot = build_management_snapshot(self.conn, date(2026,11,1), date(2026,11,30))
        self.conn.set_trace_callback(None)
        self.assertEqual(snapshot["current"]["revenue"], Decimal("500"))
        self.assertEqual(snapshot["previous"]["revenue"], Decimal("200"))
        self.assertEqual(snapshot["cash"], Decimal("1350"))
        grouped = [
            sql for sql in statements
            if "GROUP BY a.id,a.code,a.name_ar,a.account_type,a.is_group" in sql
        ]
        self.assertEqual(len(grouped), 1)

    def test_cash_forecast_groups_outstanding_invoices_by_due_week_exactly(self):
        self.conn.executescript("""
            CREATE TABLE sales_invoices(
                id INTEGER PRIMARY KEY,status TEXT,invoice_date TEXT,due_date TEXT,
                total_amount TEXT,paid_amount TEXT,credited_amount TEXT
            );
            CREATE TABLE supplier_bills(
                id INTEGER PRIMARY KEY,status TEXT,bill_date TEXT,due_date TEXT,
                total_amount TEXT,paid_amount TEXT,credited_amount TEXT
            );
            INSERT INTO sales_invoices VALUES
                (1,'posted','2026-11-10','2026-11-08','1000.00','200.00','100.00'),
                (2,'posted','2026-11-10','2026-11-25','300.00','0','0'),
                (3,'draft','2026-11-10','2026-11-12','900.00','0','0');
            INSERT INTO supplier_bills VALUES
                (1,'posted','2026-11-10','2026-11-26','500.00','100.00','0');
        """)
        forecast = cash_forecast(self.conn, date(2026,11,11), 8)
        self.assertEqual(forecast["weeks"][0]["receipts"], Decimal("700.00"))
        self.assertEqual(forecast["weeks"][2]["receipts"], Decimal("300.00"))
        self.assertEqual(forecast["weeks"][2]["payments"], Decimal("400.00"))

    def test_management_snapshot_explicitly_excludes_closing_entries_from_period_totals(self):
        from unittest.mock import patch
        from management_dashboard import dashboard_period_balances as original

        with patch("management_dashboard.dashboard_period_balances", wraps=original) as balances:
            snapshot = build_management_snapshot(
                self.conn, date(2026,11,1), date(2026,11,30)
            )
        self.assertTrue(balances.call_args.kwargs["exclude_closing_entries"])
        self.assertEqual(snapshot["current"]["net_profit"], Decimal("100"))
        # The balance-sheet snapshot still includes all posted entries as of the date.
        self.assertEqual(snapshot["cash"], Decimal("1350"))

    def test_period_results_exclude_year_end_closing_journal(self):
        self.conn.execute(
            "INSERT INTO journal_entries(id,entry_date,status,entry_type) VALUES(40,'2026-11-30','posted','year_end_closing')"
        )
        self.conn.executemany(
            "INSERT INTO journal_lines(entry_id,account_id,debit,credit) VALUES(40,?,?,?)",
            [(7,"500","0"),(8,"0","300"),(9,"0","100"),(1,"0","100")]
        )
        snapshot = build_management_snapshot(self.conn,date(2026,11,1),date(2026,11,30))
        self.assertEqual(snapshot["current"]["revenue"],Decimal("500"))
        self.assertEqual(snapshot["current"]["expenses"],Decimal("400"))
        self.assertEqual(snapshot["current"]["net_profit"],Decimal("100"))

    def test_closed_full_year_keeps_management_profit_and_balanced_position(self):
        self.conn.execute(
            "INSERT INTO journal_entries(id,entry_date,status,entry_type) "
            "VALUES(40,'2026-12-31','posted','year_end_closing')"
        )
        # Close 2026 P&L: revenue 700, expenses 570, net profit 130.
        self.conn.executemany(
            "INSERT INTO journal_lines(entry_id,account_id,debit,credit) VALUES(40,?,?,?)",
            [(7,"700","0"),(8,"0","420"),(9,"0","150"),(6,"0","130")],
        )
        snapshot = build_management_snapshot(
            self.conn, date(2026,1,1), date(2026,12,31)
        )
        self.assertEqual(snapshot["current"]["revenue"], Decimal("700"))
        self.assertEqual(snapshot["current"]["expenses"], Decimal("570"))
        self.assertEqual(snapshot["current"]["net_profit"], Decimal("130"))
        self.assertEqual(snapshot["cash"], Decimal("1350"))
        self.assertEqual(snapshot["total_equity"], Decimal("1630"))
        self.assertTrue(snapshot["balance_ok"])

    def test_prior_year_closing_entry_is_included_in_next_year_opening_balances(self):
        from management_dashboard import dashboard_period_balances

        self.conn.execute(
            "INSERT INTO journal_entries(id,entry_date,status,entry_type) "
            "VALUES(41,'2026-12-31','posted','year_end_closing')"
        )
        self.conn.executemany(
            "INSERT INTO journal_lines(entry_id,account_id,debit,credit) VALUES(41,?,?,?)",
            [(7,"700","0"),(8,"0","420"),(9,"0","150"),(6,"0","130")],
        )
        _current, _previous, _as_of, opening = dashboard_period_balances(
            self.conn, "2027-01-01", "2027-01-31",
            "2026-12-01", "2026-12-31", exclude_closing_entries=True,
        )
        self.assertEqual(opening[6]["credit"], Decimal("1630"))

    def test_position_and_liquidity(self):
        s=build_management_snapshot(self.conn,date(2026,11,1),date(2026,11,30))
        self.assertEqual(s["gross_margin"],Decimal("40.00"))
        self.assertEqual(s["cash"],Decimal("1350"))
        self.assertEqual(s["receivables"],Decimal("200"))
        # Commercial chart: 1150 is inventory (500 - 120 - 300 + 100 = 180).
        self.assertEqual(s["inventory"],Decimal("180"))
        self.assertEqual(s["payables"],Decimal("100"))
        self.assertEqual(s["current_ratio"],Decimal("17.30"))
        self.assertEqual(s["quick_ratio"],Decimal("15.50"))
        self.assertEqual(s["working_capital"],Decimal("1630"))
        self.assertEqual(s["ratios"]["current_ratio"], Decimal("17.30"))
        self.assertEqual(s["ratios"]["quick_ratio"], Decimal("15.50"))
        self.assertEqual(s["ratios"]["gross_margin"], Decimal("40.00"))
        self.assertEqual(s["ratios"]["net_profit_margin"], Decimal("20.00"))
        self.assertEqual(s["ratios"]["debt_ratio"], Decimal("5.78"))
        self.assertEqual(s["ratios"]["equity_ratio"], Decimal("86.71"))
        self.assertTrue(s["balance_ok"])

    def test_activity_mapping_changes_inventory_accounts(self):
        self.conn.execute("UPDATE company_settings SET setting_value='industrial' WHERE setting_key='activity'")
        initialize_management_dashboard_schema(self.conn, 'industrial')
        s=build_management_snapshot(self.conn,date(2026,11,1),date(2026,11,30))
        # Industrial chart: 1130 is raw-material inventory, so the 200 debit is stock.
        self.assertEqual(s["inventory"],Decimal("380"))
        self.assertEqual(s["receivables"],Decimal("0"))

    def test_legacy_default_roles_are_reseeded_for_industrial_activity(self):
        self.conn.execute("INSERT INTO accounts(id,code,name_ar,account_type,is_group,active) VALUES"
                          "(10,'1140','إنتاج تحت التشغيل','asset',0,1),"
                          "(11,'1160','قطع الغيار','asset',0,1)")
        # Simulate an older installation with auto-seeded commercial role rows
        # but without the metadata that distinguishes user-customized mappings.
        self.conn.execute("DELETE FROM management_dashboard_meta")
        self.conn.execute("UPDATE company_settings SET setting_value='industrial' WHERE setting_key='activity'")
        initialize_management_dashboard_schema(self.conn, 'industrial')
        roles = load_management_account_roles(self.conn)
        self.assertEqual(roles["cash"], {1})
        self.assertEqual(roles["receivables"], {2})
        self.assertEqual(roles["inventory"], {3, 4, 10, 11})
        snapshot = build_management_snapshot(self.conn, date(2026,11,1), date(2026,11,30))
        self.assertEqual(snapshot["cash"], Decimal("1350"))
        self.assertEqual(snapshot["receivables"], Decimal("0"))
        self.assertEqual(snapshot["inventory"], Decimal("380"))

    def test_industrial_chart_roles_separate_customers_and_all_inventory_classes(self):
        self.conn.execute("INSERT INTO accounts(id,code,name_ar,account_type,is_group,active) VALUES"
                          "(10,'1140','إنتاج تحت التشغيل','asset',0,1),"
                          "(11,'1160','قطع الغيار','asset',0,1)")
        self.conn.execute("UPDATE company_settings SET setting_value='industrial' WHERE setting_key='activity'")
        initialize_management_dashboard_schema(self.conn, 'industrial')
        self.conn.execute("INSERT INTO journal_entries(id,entry_date,status) VALUES(30,'2026-11-15','posted')")
        self.conn.executemany(
            "INSERT INTO journal_lines(entry_id,account_id,debit,credit) VALUES(30,?,?,?)",
            [(2,"25","0"),(10,"40","0"),(11,"60","0")])
        snapshot=build_management_snapshot(self.conn,date(2026,11,1),date(2026,11,30))
        self.assertEqual(snapshot["cash"],Decimal("1350"))
        self.assertEqual(snapshot["receivables"],Decimal("25"))
        self.assertEqual(snapshot["inventory"],Decimal("480"))
        self.assertEqual(snapshot["account_roles"]["inventory"],
                         {3,4,10,11})

    def test_medical_chart_roles_include_insurance_receivables_and_lab_stock(self):
        self.conn.execute("INSERT INTO accounts(id,code,name_ar,account_type,is_group,active) VALUES"
                          "(10,'1140','مطالبات شركات التأمين','asset',0,1),"
                          "(11,'1160','مخزون المعامل','asset',0,1),"
                          "(12,'2100','الموردون','liability',0,1)")
        self.conn.execute("UPDATE company_settings SET setting_value='medical' WHERE setting_key='activity'")
        initialize_management_dashboard_schema(self.conn, 'medical')
        self.conn.execute("INSERT INTO journal_entries(id,entry_date,status) VALUES(31,'2026-11-15','posted')")
        self.conn.executemany(
            "INSERT INTO journal_lines(entry_id,account_id,debit,credit) VALUES(31,?,?,?)",
            [(10,"40","0"),(11,"60","0"),(12,"0","75")])
        snapshot=build_management_snapshot(self.conn,date(2026,11,1),date(2026,11,30))
        self.assertEqual(snapshot["cash"],Decimal("1350"))
        self.assertEqual(snapshot["receivables"],Decimal("240"))
        self.assertEqual(snapshot["inventory"],Decimal("240"))
        self.assertEqual(snapshot["payables"],Decimal("75"))
        self.assertEqual(snapshot["account_roles"]["inventory"],{4,11})

    def test_new_default_roles_are_backfilled_without_resetting_existing_mappings(self):
        self.conn.execute("INSERT INTO accounts(id,code,name_ar,account_type,is_group,active) VALUES"
                          "(20,'1165','ضريبة مدخلات','asset',0,1)")
        initialize_management_dashboard_schema(self.conn, 'commercial')
        roles=load_management_account_roles(self.conn)
        self.assertIn(20,roles["current_assets"])
        self.assertNotIn("1165",ACTIVITY_ACCOUNT_ROLES["commercial"]["quick_assets"])

    def test_custom_account_mapping_works_without_standard_account_codes(self):
        self.conn.execute("INSERT INTO accounts(id,code,name_ar,account_type,is_group,active) VALUES(10,'9999','خزينة فرع','asset',0,1)")
        self.conn.execute("INSERT INTO journal_lines(entry_id,account_id,debit,credit) VALUES(1,10,'77.25','0')")
        load_management_account_roles(self.conn)
        self.conn.execute("DELETE FROM management_account_roles WHERE role='cash'")
        self.conn.execute("INSERT INTO management_account_roles(account_id,role) VALUES(10,'cash')")
        snapshot = build_management_snapshot(self.conn,date(2026,11,1),date(2026,11,30))
        self.assertEqual(snapshot["cash"], Decimal("77.25"))

    def test_clearing_all_role_mappings_does_not_restore_defaults(self):
        self.conn.execute("DELETE FROM management_account_roles")
        self.conn.execute("INSERT INTO management_dashboard_meta(meta_key,meta_value) VALUES('roles_customized','1')")
        initialize_management_dashboard_schema(self.conn, 'commercial')
        roles = load_management_account_roles(self.conn)
        self.assertTrue(all(not ids for ids in roles.values()))


    def test_cash_forecast_returns_eight_weeks_and_minimum_threshold(self):
        self.conn.execute("INSERT INTO company_settings VALUES('minimum_cash_balance','1400')")
        forecast = cash_forecast(self.conn, date(2026, 11, 11), 8)
        self.assertEqual(forecast["opening"], Decimal("1350"))
        self.assertEqual(len(forecast["weeks"]), 8)
        self.assertTrue(forecast["warning"])
        self.assertEqual(forecast["minimum"], Decimal("1400"))

    def test_unusual_journal_flags_entries_in_last_two_days(self):
        self.conn.execute("INSERT INTO journal_entries(id,entry_date,status) VALUES(20,'2026-11-29','posted')")
        self.conn.execute("INSERT INTO journal_lines(entry_id,account_id,debit,credit) VALUES(20,9,'25','0')")
        self.conn.execute("INSERT INTO journal_lines(entry_id,account_id,debit,credit) VALUES(20,1,'0','25')")
        flagged = unusual_journal_entries(self.conn, date(2026, 11, 1), date(2026, 11, 30))
        target = next(x for x in flagged if x["id"] == 20)
        self.assertIn("قيد في آخر يومين من الفترة", target["reasons"])

    def test_reversed_dates_rejected(self):
        with self.assertRaises(ValueError):
            build_management_snapshot(self.conn,date(2026,11,30),date(2026,11,1))

    def test_unusual_journal_totals_preserve_decimal_precision(self):
        self.conn.execute("INSERT INTO journal_entries(id,entry_date,status) VALUES(21,'2026-11-30','posted')")
        self.conn.execute("INSERT INTO journal_lines(entry_id,account_id,debit,credit) VALUES(21,9,'0.10','0')")
        self.conn.execute("INSERT INTO journal_lines(entry_id,account_id,debit,credit) VALUES(21,1,'0','0.10')")
        flagged = unusual_journal_entries(self.conn, date(2026,11,1), date(2026,11,30))
        target = next(x for x in flagged if x["id"] == 21)
        self.assertEqual(target["debit"], Decimal("0.10"))
        self.assertEqual(target["credit"], Decimal("0.10"))

    def test_unusual_journal_detection_batches_history_queries_and_keeps_decimal_math(self):
        self.conn.execute("INSERT INTO journal_entries(id,entry_date,status) VALUES(23,'2026-11-15','posted')")
        self.conn.execute("INSERT INTO journal_lines(entry_id,account_id,debit,credit) VALUES(23,9,'300.00','0')")
        self.conn.execute("INSERT INTO journal_lines(entry_id,account_id,debit,credit) VALUES(23,1,'0','300.00')")
        for entry_id in range(100, 200):
            self.conn.execute(
                "INSERT INTO journal_entries(id,entry_date,status) VALUES(?, '2026-11-15', 'posted')",
                (entry_id,),
            )
            self.conn.execute(
                "INSERT INTO journal_lines(entry_id,account_id,debit,credit) VALUES(?,9,'1.00','0')",
                (entry_id,),
            )
            self.conn.execute(
                "INSERT INTO journal_lines(entry_id,account_id,debit,credit) VALUES(?,1,'0','1.00')",
                (entry_id,),
            )

        statements = []
        self.conn.set_trace_callback(statements.append)
        flagged = unusual_journal_entries(self.conn, date(2026,11,1), date(2026,11,30))
        self.conn.set_trace_callback(None)

        target = next(x for x in flagged if x["id"] == 23)
        self.assertTrue(any(reason.startswith("مبلغ مرتفع عن متوسط حساب 6400") for reason in target["reasons"]))
        self.assertEqual(target["debit"], Decimal("300.00"))
        journal_selects = [
            sql for sql in statements
            if sql.lstrip().upper().startswith("SELECT") and "JOURNAL_LINES" in sql.upper()
        ]
        per_entry_selects = [
            sql for sql in statements
            if "WHERE jl.entry_id=" in sql or "WHERE h.entry_id=" in sql
        ]
        self.assertEqual(len(journal_selects), 3)
        self.assertEqual(per_entry_selects, [])


    def test_cached_dashboard_includes_and_invalidates_expensive_sections(self):
        from unittest.mock import patch
        from management_dashboard import cached_management_snapshot, _SNAPSHOT_CACHE, _SNAPSHOT_CACHE_LOCK

        with _SNAPSHOT_CACHE_LOCK:
            _SNAPSHOT_CACHE.clear()
        self.conn.execute("""CREATE TABLE sales_invoices(
            id INTEGER PRIMARY KEY, status TEXT, invoice_date TEXT,
            total_amount TEXT, paid_amount TEXT, credited_amount TEXT
        )""")
        self.conn.execute("""CREATE TABLE supplier_bills(
            id INTEGER PRIMARY KEY, status TEXT, bill_date TEXT,
            total_amount TEXT, paid_amount TEXT, credited_amount TEXT
        )""")
        initialize_management_dashboard_schema(self.conn, "commercial")

        with patch("management_dashboard.cash_forecast", return_value={"weeks": []}) as forecast, patch(
            "management_dashboard.unusual_journal_entries", return_value=[]
        ) as unusual:
            first = cached_management_snapshot(self.conn, date(2026,11,1), date(2026,11,30))
            second = cached_management_snapshot(self.conn, date(2026,11,1), date(2026,11,30))
            self.assertEqual(second["current"]["revenue"], first["current"]["revenue"])
            self.assertIn("cash_forecast", first)
            self.assertIn("unusual_entries", first)
            self.assertEqual(forecast.call_count, 1)
            self.assertEqual(unusual.call_count, 1)

            self.conn.execute("""INSERT INTO sales_invoices
                (id,status,invoice_date,total_amount,paid_amount,credited_amount)
                VALUES(1,'posted','2026-11-20','100','0','0')""")
            refreshed = cached_management_snapshot(self.conn, date(2026,11,1), date(2026,11,30))
            self.assertEqual(forecast.call_count, 2)
            self.assertEqual(unusual.call_count, 2)
            self.assertIn("cash_forecast", refreshed)

    def test_cached_snapshot_is_invalidated_by_new_journal_lines(self):
        from management_dashboard import cached_management_snapshot
        first = cached_management_snapshot(self.conn, date(2026,11,1), date(2026,11,30))
        self.assertEqual(first["current"]["revenue"], Decimal("500"))
        self.conn.execute("INSERT INTO journal_entries(id,entry_date,status) VALUES(22,'2026-11-20','posted')")
        self.conn.execute("INSERT INTO journal_lines(entry_id,account_id,debit,credit) VALUES(22,1,'0','0')")
        self.conn.execute("INSERT INTO journal_lines(entry_id,account_id,debit,credit) VALUES(22,7,'0','25')")
        second = cached_management_snapshot(self.conn, date(2026,11,1), date(2026,11,30))
        self.assertEqual(second["current"]["revenue"], Decimal("525"))


if __name__ == "__main__":
    unittest.main()
