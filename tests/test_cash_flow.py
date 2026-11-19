import sqlite3
import tempfile
import unittest
from pathlib import Path

from flask import Flask

import financial_planning
from accounting_core import register_decimal_aggregates


class CashFlowReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "cashflow.db"
        self.original_connect = financial_planning._connect

        def connect():
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            register_decimal_aggregates(conn)
            return conn

        financial_planning._connect = connect
        conn = connect()
        conn.executescript("""
          CREATE TABLE accounts(id INTEGER PRIMARY KEY,code TEXT,name_ar TEXT,account_type TEXT,is_group INTEGER DEFAULT 0,active INTEGER DEFAULT 1);
          CREATE TABLE journal_entries(id INTEGER PRIMARY KEY,entry_date TEXT,status TEXT);
          CREATE TABLE journal_lines(id INTEGER PRIMARY KEY,entry_id INTEGER,account_id INTEGER,debit TEXT,credit TEXT);
          CREATE TABLE management_account_roles(role TEXT,account_id INTEGER);
          CREATE TABLE management_dashboard_meta(meta_key TEXT PRIMARY KEY,meta_value TEXT);
          CREATE TABLE cash_flow_account_mappings(
            account_id INTEGER PRIMARY KEY,category TEXT NOT NULL
          );
          INSERT INTO accounts(id,code,name_ar,account_type,is_group) VALUES
            (1,'1110','الصندوق','asset',0),
            (2,'3100','رأس المال','equity',0),
            (3,'4100','إيرادات المبيعات','revenue',0),
            (4,'6100','مصروفات تشغيلية','expense',0),
            (5,'1240','معدات','asset',0),
            (6,'2200','قرض طويل الأجل','liability',0);
          INSERT INTO management_account_roles VALUES('cash',1);
          INSERT INTO journal_entries VALUES
            (1,'2026-10-31','posted'),(2,'2026-11-05','posted'),
            (3,'2026-11-08','posted'),(4,'2026-11-12','posted'),
            (5,'2026-11-15','posted');
          INSERT INTO journal_lines VALUES
            (1,1,1,'1000','0'),(2,1,2,'0','1000'),
            (3,2,1,'300','0'),(4,2,3,'0','300'),
            (5,3,4,'80','0'),(6,3,1,'0','80'),
            (7,4,5,'200','0'),(8,4,1,'0','200'),
            (9,5,1,'500','0'),(10,5,6,'0','500');
        """)
        conn.commit()
        conn.close()

        templates = Path(__file__).resolve().parents[1] / "templates"
        self.app = Flask(__name__, template_folder=str(templates))
        self.app.add_url_rule("/__test_global_search", endpoint="global_search", view_func=lambda: "")
        self.app.add_url_rule(
            "/reports/export/<report>/excel", endpoint="report_exports.export_report_excel",
            view_func=lambda report: ""
        )
        self.app.secret_key = "test"
        self.app.jinja_env.globals["csrf_token"] = lambda: "test-token"
        self.app.jinja_env.filters["money"] = lambda value: f"{float(value or 0):,.2f}"
        self.app.context_processor(lambda: {
            "permissions": {"cashflow.view"},
            "license_info": {"status": "demo"},
        })
        self.app.add_url_rule("/change-password", endpoint="change_password", view_func=lambda: "")
        self.app.add_url_rule("/logout", endpoint="logout", view_func=lambda: "")
        self.app.register_blueprint(financial_planning.planning_bp)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["permissions"] = ["cashflow.view"]

    def tearDown(self):
        financial_planning._connect = self.original_connect
        self.temp.cleanup()

    def test_existing_report_viewer_can_open_cash_flow_without_new_permission_flag(self):
        with self.client.session_transaction() as session:
            session["permissions"] = ["reports.view"]
        response = self.client.get(
            "/reports/cash-flow?date_from=2026-11-01&date_to=2026-11-30"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("قائمة التدفقات النقدية", response.get_data(as_text=True))

    def test_indirect_method_is_available_and_shows_reconciliation_lines(self):
        response = self.client.get(
            "/reports/cash-flow?date_from=2026-11-01&date_to=2026-11-30"
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("الطريقة غير المباشرة", body)
        self.assertIn("صافي الربح قبل التدفقات النقدية", body)
        self.assertIn("التغير في العملاء والمدينين", body)
        self.assertIn("صافي التدفق النقدي من التشغيل", body)

    def test_direct_method_remains_selectable(self):
        response = self.client.get(
            "/reports/cash-flow?date_from=2026-11-01&date_to=2026-11-30&method=direct"
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("الطريقة المباشرة", body)
        self.assertNotIn("صافي الربح قبل التدفقات النقدية", body)

    def test_explicit_account_mappings_drive_investing_and_financing_sections(self):
        conn = financial_planning._connect()
        conn.execute("INSERT INTO cash_flow_account_mappings(account_id,category) VALUES(5,'investing')")
        conn.execute("INSERT INTO cash_flow_account_mappings(account_id,category) VALUES(6,'financing')")
        conn.commit()
        snapshot = financial_planning.build_cash_flow_snapshot(
            conn, "2026-11-01", "2026-11-30"
        )
        conn.close()
        self.assertEqual(snapshot["sections"]["investing"]["out"], financial_planning.Decimal("200"))
        self.assertEqual(snapshot["sections"]["financing"]["in"], financial_planning.Decimal("500"))
        self.assertEqual(snapshot["unmapped_account_count"], 2)
        self.assertEqual(
            {item["code"] for item in snapshot["unmapped_accounts"]},
            {"4100", "6100"},
        )

    def test_supplier_payment_allocated_to_asset_bill_is_investing_cash_outflow(self):
        conn = financial_planning._connect()
        conn.execute("ALTER TABLE journal_entries ADD COLUMN reference TEXT")
        conn.execute("INSERT INTO accounts(id,code,name_ar,account_type,is_group) VALUES(8,'2100','الموردون','liability',0)")
        conn.execute("INSERT INTO journal_entries(id,entry_date,status,reference) VALUES(6,'2026-11-20','posted','PAY-ASSET-MIX')")
        conn.execute("INSERT INTO journal_lines(id,entry_id,account_id,debit,credit) VALUES(11,6,8,'1500','0')")
        conn.execute("INSERT INTO journal_lines(id,entry_id,account_id,debit,credit) VALUES(12,6,1,'0','1500')")
        conn.executescript("""
            CREATE TABLE supplier_bills(id INTEGER PRIMARY KEY,bill_kind TEXT);
            CREATE TABLE supplier_payments(id INTEGER PRIMARY KEY,journal_entry_id INTEGER);
            CREATE TABLE supplier_payment_allocations(payment_id INTEGER,bill_id INTEGER,amount TEXT);
            INSERT INTO supplier_bills VALUES(1,'asset'),(2,'expense');
            INSERT INTO supplier_payments VALUES(1,6);
            INSERT INTO supplier_payment_allocations VALUES(1,1,'1000'),(1,2,'500');
        """)
        snapshot = financial_planning.build_cash_flow_snapshot(
            conn, "2026-11-01", "2026-11-30"
        )
        conn.close()
        self.assertEqual(snapshot["sections"]["investing"]["out"], financial_planning.Decimal("1200.00"))
        self.assertEqual(snapshot["sections"]["operating"]["out"], financial_planning.Decimal("580.00"))
        self.assertEqual(snapshot["reconciliation_difference"], financial_planning.Decimal("0"))

    def test_indirect_method_does_not_hide_variance_in_a_plug_adjustment(self):
        conn = financial_planning._connect()
        snapshot = financial_planning.build_cash_flow_indirect_snapshot(
            conn, "2026-11-01", "2026-11-30"
        )
        conn.close()
        labels = {item["label"] for item in snapshot["indirect_items"]}
        self.assertNotIn("تسويات أخرى / بنود تحتاج تصنيفًا", labels)
        self.assertEqual(snapshot["indirect_reconciliation"], financial_planning.Decimal("0"))
        self.assertEqual(snapshot["reconciliation_difference"], financial_planning.Decimal("0"))

    def test_indirect_method_infers_unmapped_current_assets_and_liabilities(self):
        conn = financial_planning._connect()
        conn.execute("ALTER TABLE accounts ADD COLUMN parent_id INTEGER")
        conn.executemany(
            "INSERT INTO accounts(id,code,name_ar,account_type,is_group) VALUES(?,?,?,?,?)",
            [
                (7,"1100","الأصول المتداولة","asset",1),
                (8,"2100","الالتزامات المتداولة","liability",1),
                (9,"1260","تأمينات لدى الغير","asset",0),
                (10,"2150","مصروفات مستحقة","liability",0),
            ],
        )
        conn.execute("UPDATE accounts SET parent_id=7 WHERE id=9")
        conn.execute("UPDATE accounts SET parent_id=8 WHERE id=10")
        conn.execute(
            "INSERT INTO journal_entries VALUES(6,'2026-11-20','posted')"
        )
        conn.executemany(
            "INSERT INTO journal_lines VALUES(?,?,?,?,?)",
            [
                (11,6,9,"100","0"),
                (12,6,1,"0","100"),
            ],
        )
        conn.execute(
            "INSERT INTO journal_entries VALUES(7,'2026-11-21','posted')"
        )
        conn.executemany(
            "INSERT INTO journal_lines VALUES(?,?,?,?,?)",
            [
                (13,7,1,"80","0"),
                (14,7,10,"0","80"),
            ],
        )
        snapshot = financial_planning.build_cash_flow_indirect_snapshot(
            conn, "2026-11-01", "2026-11-30"
        )
        conn.close()
        self.assertEqual(snapshot["indirect_operating_net"], financial_planning.Decimal("200"))
        self.assertEqual(snapshot["reconciliation_difference"], financial_planning.Decimal("0"))
        self.assertFalse(snapshot["indirect_mapping_warning"])
        self.assertIn(9, snapshot["inferred_operating_account_ids"])
        self.assertIn(10, snapshot["inferred_operating_account_ids"])
        remaining_unmapped = {row["code"] for row in snapshot["unmapped_accounts"]}
        self.assertNotIn("1260", remaining_unmapped)
        self.assertNotIn("2150", remaining_unmapped)

    def test_indirect_cash_flow_excludes_copied_later_year_opening_entry(self):
        conn = financial_planning._connect()
        conn.execute(
            "INSERT INTO accounts(id,code,name_ar,account_type,is_group) "
            "VALUES(7,'2130','مستحقات تشغيلية','liability',0)"
        )
        conn.execute("INSERT INTO journal_entries VALUES(6,'2026-12-31','posted')")
        conn.executemany(
            "INSERT INTO journal_lines VALUES(?,?,?,?,?)",
            [(11,6,1,"200","0"), (12,6,7,"0","200")],
        )
        conn.execute("INSERT INTO journal_entries VALUES(7,'2027-01-01','posted')")
        conn.executemany(
            "INSERT INTO journal_lines VALUES(?,?,?,?,?)",
            [(13,7,1,"200","0"), (14,7,7,"0","200")],
        )
        conn.executescript("""
            CREATE TABLE financial_years(
                id INTEGER PRIMARY KEY, year_name TEXT, start_date TEXT, end_date TEXT,
                status TEXT, opening_entry_id INTEGER
            );
            CREATE TABLE financial_year_journal_history(
                id INTEGER PRIMARY KEY, financial_year_id INTEGER, entry_kind TEXT,
                action TEXT, entry_id INTEGER, related_entry_id INTEGER, ledger_history_copied INTEGER
            );
            INSERT INTO financial_years
                VALUES(1,'2027','2027-01-01','2027-12-31','open',7);
            INSERT INTO financial_year_journal_history
                (financial_year_id,entry_kind,action,entry_id,ledger_history_copied)
                VALUES(1,'opening','created',7,1);
        """)
        snapshot = financial_planning.build_cash_flow_indirect_snapshot(
            conn, "2027-01-02", "2027-01-31"
        )
        conn.close()
        other_liabilities = next(
            item for item in snapshot["indirect_items"]
            if item["label"] == "التغير في الالتزامات المتداولة الأخرى"
        )
        self.assertEqual(other_liabilities["amount"], financial_planning.Decimal("0"))
        self.assertEqual(snapshot["opening"], financial_planning.Decimal("1720"))
        self.assertEqual(snapshot["closing"], financial_planning.Decimal("1720"))
        self.assertEqual(snapshot["indirect_operating_net"], financial_planning.Decimal("0"))
        self.assertEqual(snapshot["indirect_reconciliation"], financial_planning.Decimal("0"))
        self.assertEqual(snapshot["reconciliation_difference"], financial_planning.Decimal("0"))

    def test_unparented_payroll_payable_and_grni_are_included_in_indirect_working_capital(self):
        conn = financial_planning._connect()
        conn.executemany(
            "INSERT INTO accounts(id,code,name_ar,account_type,is_group) VALUES(?,?,?,?,0)",
            [
                (7, "2130", "مستحقات المرتبات", "liability"),
                (8, "2190", "بضاعة مستلمة غير مفوترة GRNI", "liability"),
            ],
        )
        conn.execute("INSERT INTO journal_entries VALUES(6,'2026-11-20','posted')")
        conn.executemany(
            "INSERT INTO journal_lines VALUES(?,?,?,?,?)",
            [
                (11, 6, 1, "5269.23", "0"),
                (12, 6, 7, "0", "5269.23"),
            ],
        )
        conn.execute("INSERT INTO journal_entries VALUES(7,'2026-11-21','posted')")
        conn.executemany(
            "INSERT INTO journal_lines VALUES(?,?,?,?,?)",
            [
                (13, 7, 1, "20", "0"),
                (14, 7, 8, "0", "20"),
            ],
        )
        direct = financial_planning.build_cash_flow_snapshot(
            conn, "2026-11-01", "2026-11-30"
        )
        indirect = financial_planning.build_cash_flow_indirect_snapshot(
            conn, "2026-11-01", "2026-11-30"
        )
        conn.close()
        self.assertEqual(direct["reconciliation_difference"], financial_planning.Decimal("0"))
        self.assertEqual(indirect["indirect_reconciliation"], financial_planning.Decimal("0"))
        self.assertEqual(indirect["reconciliation_difference"], financial_planning.Decimal("0"))
        other_liabilities = next(
            item for item in indirect["indirect_items"]
            if item["label"] == "التغير في الالتزامات المتداولة الأخرى"
        )
        self.assertEqual(other_liabilities["amount"], financial_planning.Decimal("5289.23"))
        self.assertNotIn("2130", {row["code"] for row in indirect["unmapped_accounts"]})
        self.assertNotIn("2190", {row["code"] for row in indirect["unmapped_accounts"]})

    def test_general_expense_account_linked_to_asset_is_not_added_back_as_depreciation(self):
        conn = financial_planning._connect()
        conn.execute("CREATE TABLE fixed_assets(id INTEGER PRIMARY KEY, expense_account_id INTEGER)")
        conn.execute("INSERT INTO fixed_assets(id,expense_account_id) VALUES(1,4)")
        conn.execute("""CREATE TABLE asset_depreciations(
            id INTEGER PRIMARY KEY, asset_id INTEGER, period TEXT, amount TEXT,
            journal_entry_id INTEGER
        )""")
        conn.execute("INSERT INTO asset_depreciations VALUES(1,1,'2026-11','80',3)")
        # This asset points at a general operating expense account. Its unrelated
        # administrative posting must not be classified as depreciation.
        conn.execute("INSERT INTO journal_entries VALUES(6,'2026-11-22','posted')")
        conn.executemany(
            "INSERT INTO journal_lines VALUES(?,?,?,?,?)",
            [(11,6,4,"25","0"), (12,6,1,"0","25")],
        )
        snapshot = financial_planning.build_cash_flow_indirect_snapshot(
            conn, "2026-11-01", "2026-11-30"
        )
        conn.close()
        adjustment = next(
            item for item in snapshot["indirect_items"]
            if item["label"] == "إضافة الإهلاك والمصروفات غير النقدية المحددة"
        )
        self.assertEqual(adjustment["amount"], financial_planning.Decimal("80"))

    def test_year_end_closing_entry_is_not_added_back_as_depreciation(self):
        conn = financial_planning._connect()
        conn.execute("ALTER TABLE journal_entries ADD COLUMN entry_type TEXT")
        conn.execute("INSERT INTO accounts(id,code,name_ar,account_type,is_group) VALUES(7,'6400','مصروف إهلاك','expense',0)")
        conn.execute("INSERT INTO accounts(id,code,name_ar,account_type,is_group) VALUES(8,'3200','أرباح مرحلة','equity',0)")
        conn.execute("CREATE TABLE fixed_assets(id INTEGER PRIMARY KEY, expense_account_id INTEGER)")
        conn.execute("INSERT INTO fixed_assets(id,expense_account_id) VALUES(1,7)")
        conn.execute("INSERT INTO journal_entries(id,entry_date,status,entry_type) VALUES(6,'2026-11-30','posted','year_end_closing')")
        conn.executemany(
            "INSERT INTO journal_lines VALUES(?,?,?,?,?)",
            [(11,6,8,"80","0"), (12,6,7,"0","80")],
        )
        snapshot = financial_planning.build_cash_flow_indirect_snapshot(
            conn, "2026-11-01", "2026-11-30"
        )
        conn.close()
        adjustment = next(
            item for item in snapshot["indirect_items"]
            if item["label"] == "إضافة الإهلاك والمصروفات غير النقدية المحددة"
        )
        self.assertEqual(adjustment["amount"], financial_planning.Decimal("0"))

    def test_non_cash_provision_expense_is_offset_by_current_provision_liability(self):
        conn = financial_planning._connect()
        baseline = financial_planning.build_cash_flow_indirect_snapshot(
            conn, "2026-11-01", "2026-11-30"
        )
        conn.executemany(
            "INSERT INTO accounts(id,code,name_ar,account_type,is_group) VALUES(?,?,?,?,0)",
            [
                (7, "2250", "مخصص التزامات متداولة", "liability"),
                (8, "6200", "مصروف تكوين مخصص", "expense"),
            ],
        )
        conn.execute("INSERT INTO journal_entries VALUES(6,'2026-11-18','posted')")
        conn.executemany(
            "INSERT INTO journal_lines VALUES(?,?,?,?,?)",
            [
                (11, 6, 8, "40", "0"),
                (12, 6, 7, "0", "40"),
            ],
        )
        after = financial_planning.build_cash_flow_indirect_snapshot(
            conn, "2026-11-01", "2026-11-30"
        )
        conn.close()

        self.assertEqual(
            after["net_profit"], baseline["net_profit"] - financial_planning.Decimal("40")
        )
        self.assertEqual(after["indirect_operating_net"], baseline["indirect_operating_net"])
        provision_change = next(
            item for item in after["indirect_items"]
            if item["label"] == "التغير في الالتزامات المتداولة الأخرى"
        )
        self.assertEqual(provision_change["amount"], financial_planning.Decimal("40"))
        self.assertEqual(after["reconciliation_difference"], financial_planning.Decimal("0"))

    def test_asset_disposal_cash_proceeds_are_classified_as_investing(self):
        conn = financial_planning._connect()
        conn.execute("ALTER TABLE journal_entries ADD COLUMN reference TEXT")
        conn.execute("INSERT INTO accounts(id,code,name_ar,account_type,is_group) VALUES(7,'1290','مجمع إهلاك','asset',0)")
        conn.execute("INSERT INTO journal_entries(id,entry_date,status,reference) VALUES(6,'2026-11-18','posted','ASSET-DISPOSAL-1')")
        conn.executemany(
            "INSERT INTO journal_lines(id,entry_id,account_id,debit,credit) VALUES(?,?,?,?,?)",
            [
                (11,6,1,"700","0"),
                (12,6,5,"0","500"),
                (13,6,7,"100","0"),
                (14,6,3,"0","300"),
            ],
        )
        conn.commit()
        snapshot = financial_planning.build_cash_flow_snapshot(
            conn, "2026-11-01", "2026-11-30"
        )
        conn.close()
        self.assertEqual(snapshot["sections"]["investing"]["in"], financial_planning.Decimal("700"))
        self.assertEqual(snapshot["sections"]["investing"]["out"], financial_planning.Decimal("200"))

    def test_asset_disposal_gain_is_removed_from_indirect_operating_cash_flow(self):
        conn = financial_planning._connect()
        conn.execute("""CREATE TABLE asset_disposals(
            id INTEGER PRIMARY KEY, disposal_date TEXT, gain_loss_amount TEXT
        )""")
        conn.execute("INSERT INTO asset_disposals VALUES(1,'2026-11-18','300')")
        snapshot = financial_planning.build_cash_flow_indirect_snapshot(
            conn, "2026-11-01", "2026-11-30"
        )
        conn.close()
        adjustment = next(
            item for item in snapshot["indirect_items"]
            if item["label"] == "استبعاد أرباح / خسائر بيع الأصول من الربح"
        )
        self.assertEqual(adjustment["amount"], financial_planning.Decimal("-300"))

    def test_cash_flow_mapping_settings_page_saves_explicit_categories(self):
        with self.client.session_transaction() as session:
            session["permissions"] = ["cashflow.view", "users.manage"]
        response = self.client.post(
            "/settings/cash-flow-mapping",
            data={"category_5":"investing", "category_6":"financing"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        conn = financial_planning._connect()
        rows = conn.execute(
            "SELECT account_id,category FROM cash_flow_account_mappings ORDER BY account_id"
        ).fetchall()
        conn.close()
        self.assertEqual([(row["account_id"],row["category"]) for row in rows],
                         [(5,"investing"),(6,"financing")])

    def test_cash_flow_reconciles_opening_movements_and_closing_cash(self):
        response = self.client.get(
            "/reports/cash-flow?date_from=2026-11-01&date_to=2026-11-30&method=direct"
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("1,000.00", body)
        self.assertIn("800.00", body)
        self.assertIn("280.00", body)
        self.assertIn("1,520.00", body)
        self.assertIn("الأنشطة التشغيلية", body)
        self.assertIn("الأنشطة الاستثمارية", body)
        self.assertIn("الأنشطة التمويلية", body)


    def test_direct_cash_flow_excludes_copied_opening_entry_inside_period(self):
        conn=financial_planning._connect()
        conn.executescript("""
          CREATE TABLE financial_years(id INTEGER PRIMARY KEY,year_name TEXT,start_date TEXT,end_date TEXT,status TEXT,opening_entry_id INTEGER);
          CREATE TABLE financial_year_journal_history(id INTEGER PRIMARY KEY,financial_year_id INTEGER,entry_kind TEXT,action TEXT,entry_id INTEGER,related_entry_id INTEGER, ledger_history_copied INTEGER);
          INSERT INTO journal_entries VALUES(6,'2027-01-01','posted');
          INSERT INTO journal_lines VALUES(11,6,1,'200','0');
          INSERT INTO journal_lines VALUES(12,6,2,'0','200');
          INSERT INTO financial_years VALUES(2,'2027','2027-01-01','2027-12-31','open',6);
          INSERT INTO financial_year_journal_history(financial_year_id,entry_kind,action,entry_id,ledger_history_copied)
            VALUES(2,'opening','created',6,1);
        """)
        snapshot=financial_planning.build_cash_flow_snapshot(conn,"2027-01-01","2027-01-31")
        conn.close()
        self.assertEqual(snapshot["net_change"],financial_planning.Decimal("0"))
        self.assertEqual(snapshot["closing"],snapshot["opening"])


if __name__ == "__main__":
    unittest.main()
