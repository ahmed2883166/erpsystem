import sqlite3
import unittest
from datetime import date
from decimal import Decimal

from management_dashboard import build_management_snapshot, load_management_account_roles, initialize_management_dashboard_schema, cash_forecast, unusual_journal_entries


class ManagementDashboardTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE accounts(id INTEGER PRIMARY KEY,code TEXT,name_ar TEXT,account_type TEXT,is_group INTEGER DEFAULT 0,active INTEGER DEFAULT 1);
            CREATE TABLE journal_entries(id INTEGER PRIMARY KEY,entry_date TEXT,status TEXT);
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
            self.conn.execute("INSERT INTO journal_entries VALUES(?,?,?)",(idx,entry_date,status))
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
        self.assertTrue(s["balance_ok"])

    def test_activity_mapping_changes_inventory_accounts(self):
        self.conn.execute("UPDATE company_settings SET setting_value='industrial' WHERE setting_key='activity'")
        initialize_management_dashboard_schema(self.conn, 'industrial')
        s=build_management_snapshot(self.conn,date(2026,11,1),date(2026,11,30))
        # Industrial chart: 1130 is raw-material inventory, so the 200 debit is stock.
        self.assertEqual(s["inventory"],Decimal("380"))
        self.assertEqual(s["receivables"],Decimal("0"))

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
        self.conn.execute("INSERT INTO journal_entries VALUES(20,'2026-11-29','posted')")
        self.conn.execute("INSERT INTO journal_lines(entry_id,account_id,debit,credit) VALUES(20,9,'25','0')")
        self.conn.execute("INSERT INTO journal_lines(entry_id,account_id,debit,credit) VALUES(20,1,'0','25')")
        flagged = unusual_journal_entries(self.conn, date(2026, 11, 1), date(2026, 11, 30))
        target = next(x for x in flagged if x["id"] == 20)
        self.assertIn("قيد في آخر يومين من الفترة", target["reasons"])

    def test_reversed_dates_rejected(self):
        with self.assertRaises(ValueError):
            build_management_snapshot(self.conn,date(2026,11,30),date(2026,11,1))


if __name__ == "__main__":
    unittest.main()
