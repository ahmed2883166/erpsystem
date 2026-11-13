import json
import sqlite3
import unittest

from activity_profiles import (
    initialize_activity_profiles_schema, list_activity_profiles,
    create_activity_profile, create_specialized_activity_profile, SPECIALIZED_ACTIVITY_PRESETS,
    set_activity_profile_active, validate_activity_profile, update_custom_activity_profile,
)
from chart_templates import seed_activity_chart, ensure_activity_chart_accounts


class ActivityProfileTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""CREATE TABLE accounts(
            id INTEGER PRIMARY KEY, code TEXT UNIQUE, name_ar TEXT,
            account_type TEXT, parent_id INTEGER, is_group INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1)""")
        initialize_activity_profiles_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def _company_settings(self):
        self.conn.execute("""CREATE TABLE company_settings(
            setting_key TEXT PRIMARY KEY, setting_value TEXT)""")

    def test_seeds_all_builtin_profiles_idempotently(self):
        initialize_activity_profiles_schema(self.conn)
        profiles = list_activity_profiles(self.conn)
        self.assertEqual({p["code"] for p in profiles},
                         {"commercial", "service", "medical", "restaurant", "industrial"})

    def test_builtin_profile_mappings_refresh_without_overwriting_custom_profiles(self):
        import json

        builtin = self.conn.execute(
            "SELECT id FROM activity_profiles WHERE code='service'"
        ).fetchone()["id"]
        self.conn.execute(
            "UPDATE activity_profiles SET account_roles_json='{}' WHERE id=?",
            (builtin,),
        )
        custom_id = create_activity_profile(
            self.conn, "custom_service", "خدمة مخصصة", "service"
        )
        self.conn.execute(
            "UPDATE activity_profiles SET account_roles_json='{}' WHERE id=?",
            (custom_id,),
        )

        initialize_activity_profiles_schema(self.conn)

        builtin_roles = json.loads(self.conn.execute(
            "SELECT account_roles_json FROM activity_profiles WHERE id=?",
            (builtin,),
        ).fetchone()["account_roles_json"])
        custom_roles = json.loads(self.conn.execute(
            "SELECT account_roles_json FROM activity_profiles WHERE id=?",
            (custom_id,),
        ).fetchone()["account_roles_json"])
        self.assertIn("5610", builtin_roles["direct_cost"])
        self.assertIn("1191", builtin_roles["current_assets"])
        self.assertEqual(custom_roles, {})

    def test_specialized_presets_create_custom_profiles_idempotently(self):
        self.assertIn("transport_logistics", SPECIALIZED_ACTIVITY_PRESETS)
        first_id = create_specialized_activity_profile(self.conn, "transport_logistics")
        second_id = create_specialized_activity_profile(self.conn, "transport_logistics")
        self.assertEqual(first_id, second_id)
        profile = next(p for p in list_activity_profiles(self.conn) if p["id"] == first_id)
        self.assertEqual(profile["base_activity"], "service")
        self.assertFalse(profile["is_builtin"])
        self.assertIn("النقل", profile["name"])

    def test_custom_profile_inherits_base_configuration(self):
        profile_id = create_activity_profile(
            self.conn, "logistics", "النقل والخدمات اللوجستية", "service", "ملف مخصص"
        )
        profile = next(p for p in list_activity_profiles(self.conn) if p["id"] == profile_id)
        self.assertEqual(profile["base_activity"], "service")
        self.assertEqual(profile["account_roles"]["cash"], ["1110"])
        self.assertFalse(profile["is_builtin"])

    def test_rejects_invalid_or_duplicate_profile_code(self):
        with self.assertRaises(ValueError):
            create_activity_profile(self.conn, "Bad-Code", "نشاط", "commercial")
        create_activity_profile(self.conn, "logistics", "نشاط", "commercial")
        with self.assertRaises(sqlite3.IntegrityError):
            create_activity_profile(self.conn, "logistics", "نشاط آخر", "commercial")

    def test_profile_validation_requires_existing_detail_accounts(self):
        profile_id = create_activity_profile(self.conn, "logistics", "لوجستيات", "service")
        profile = next(p for p in list_activity_profiles(self.conn) if p["id"] == profile_id)
        errors = validate_activity_profile(self.conn, profile)
        self.assertTrue(any("غير موجود" in error for error in errors))
        seed_activity_chart(self.conn, "service")
        errors = validate_activity_profile(self.conn, profile)
        self.assertEqual(errors, [])

    def test_custom_profile_can_be_disabled_but_builtin_cannot(self):
        profile_id = create_activity_profile(self.conn, "logistics", "لوجستيات", "service")
        set_activity_profile_active(self.conn, profile_id, False)
        self.assertFalse(next(p for p in list_activity_profiles(self.conn, True)
                              if p["id"] == profile_id)["active"])
        builtin = next(p for p in list_activity_profiles(self.conn) if p["is_builtin"])
        with self.assertRaises(ValueError):
            set_activity_profile_active(self.conn, builtin["id"], False)

    def test_active_profile_exposes_posting_and_tax_mappings_to_modules(self):
        from activity_profiles import active_posting_accounts, active_tax_roles, active_account_role_ids
        self._company_settings()
        create_activity_profile(
            self.conn, "logistics", "النقل والخدمات اللوجستية", "service"
        )
        self.conn.execute(
            "INSERT INTO company_settings(setting_key,setting_value) VALUES('activity','service')"
        )
        self.conn.execute(
            "INSERT INTO company_settings(setting_key,setting_value) VALUES('activity_profile_code','logistics')"
        )
        self.conn.execute("""INSERT INTO accounts(code,name_ar,account_type,is_group,active)
            VALUES('1110','الصندوق','asset',0,1),('1120','العملاء','asset',0,1),
                   ('2100','الموردون','liability',0,1),('4100','إيرادات الخدمات','revenue',0,1),
                   ('2130','ضريبة المخرجات','liability',0,1),('1165','ضريبة المدخلات','asset',0,1)""")
        posting = active_posting_accounts(self.conn)
        taxes = active_tax_roles(self.conn)
        self.assertEqual(posting["ar"], "1120")
        self.assertEqual(posting["supplier_ap"], "2100")
        self.assertEqual(taxes["output_vat"], "2130")
        self.assertEqual(taxes["input_vat"], "1165")
        self.assertTrue(active_account_role_ids(self.conn)["cash"])

    def test_customer_advances_role_is_current_liability_and_legacy_profiles_fallback(self):
        from activity_profiles import active_account_role_ids, get_active_activity_profile
        seed_activity_chart(self.conn, "service")
        self._company_settings()
        profile_id = create_activity_profile(
            self.conn, "legacy_logistics", "ملف لوجستي قديم", "service"
        )
        self.conn.execute(
            "INSERT INTO company_settings(setting_key,setting_value) VALUES('activity','service')"
        )
        self.conn.execute(
            "INSERT INTO company_settings(setting_key,setting_value) VALUES('activity_profile_code','legacy_logistics')"
        )
        row = self.conn.execute(
            "SELECT account_roles_json FROM activity_profiles WHERE id=?", (profile_id,)
        ).fetchone()
        old_roles = json.loads(row["account_roles_json"])
        old_roles.pop("customer_advances")
        self.conn.execute(
            "UPDATE activity_profiles SET account_roles_json=? WHERE id=?",
            (json.dumps(old_roles), profile_id),
        )

        profile = get_active_activity_profile(self.conn)
        roles = active_account_role_ids(self.conn)
        advance_id = self.conn.execute("SELECT id FROM accounts WHERE code='2160'").fetchone()["id"]
        self.assertEqual(profile["account_roles"]["customer_advances"], ["2160"])
        self.assertIn(advance_id, roles["customer_advances"])
        self.assertIn(advance_id, roles["current_liabilities"])

    def test_builtin_chart_control_roles_cover_legacy_customer_advances(self):
        from chart_templates import ACTIVITY_ACCOUNT_ROLES, CHARTS
        commercial = {row[0] for row in CHARTS["commercial"]}
        service = {row[0] for row in CHARTS["service"]}
        self.assertTrue({"2140", "2160"}.issubset(commercial))
        self.assertTrue({"2120", "2160"}.issubset(service))
        self.assertEqual(set(ACTIVITY_ACCOUNT_ROLES["commercial"]["customer_advances"]),
                         {"2140", "2160"})
        self.assertEqual(set(ACTIVITY_ACCOUNT_ROLES["service"]["customer_advances"]),
                         {"2120", "2160"})
        self.assertEqual(set(ACTIVITY_ACCOUNT_ROLES["service"]["cash"]), {"1110", "1111"})
        self.assertIn(("1111", "البنوك", "asset", "1100", 0), CHARTS["service"])

    def test_restaurant_roles_include_all_stock_and_cost_detail_accounts(self):
        from chart_templates import ACTIVITY_ACCOUNT_ROLES, CHARTS
        chart_codes = {row[0] for row in CHARTS["restaurant"]}
        self.assertTrue({"1140", "1150", "1160"}.issubset(chart_codes))
        self.assertTrue({"5100", "5200", "5300", "5400"}.issubset(chart_codes))
        self.assertTrue({"1140", "1150", "1160"}.issubset(
            set(ACTIVITY_ACCOUNT_ROLES["restaurant"]["inventory"])))
        self.assertTrue({"5100", "5200", "5300", "5400"}.issubset(
            set(ACTIVITY_ACCOUNT_ROLES["restaurant"]["direct_cost"])))

    def test_product_category_routes_restaurant_and_medical_accounts(self):
        from activity_profiles import resolve_product_account_code
        self.conn.execute("CREATE TABLE company_settings(setting_key TEXT PRIMARY KEY, setting_value TEXT)")
        self.conn.execute("CREATE TABLE stock_categories(id INTEGER PRIMARY KEY, code TEXT, name_ar TEXT)")
        self.conn.execute("""CREATE TABLE stock_products(
            id INTEGER PRIMARY KEY, category TEXT DEFAULT '', category_id INTEGER)""")
        self.conn.execute("INSERT INTO company_settings VALUES('activity','restaurant')")
        self.conn.execute("INSERT INTO stock_categories VALUES(1,'BEVERAGE','مشروبات')")
        self.conn.execute("INSERT INTO stock_products VALUES(1,'',1)")
        self.assertEqual(resolve_product_account_code(self.conn, 1, "inventory", "1140"), "1150")
        self.assertEqual(resolve_product_account_code(self.conn, 1, "cogs", "5100"), "5200")
        self.conn.execute("UPDATE company_settings SET setting_value='medical' WHERE setting_key='activity'")
        self.conn.execute("UPDATE stock_categories SET code='LAB',name_ar='مستلزمات المعامل' WHERE id=1")
        self.assertEqual(resolve_product_account_code(self.conn, 1, "inventory", "1150"), "1160")
        self.assertEqual(resolve_product_account_code(self.conn, 1, "cogs", "5300"), "5200")

    def test_unknown_product_category_keeps_configured_default_account(self):
        from activity_profiles import resolve_product_account_code
        self.conn.execute("CREATE TABLE company_settings(setting_key TEXT PRIMARY KEY, setting_value TEXT)")
        self.conn.execute("CREATE TABLE stock_categories(id INTEGER PRIMARY KEY, code TEXT, name_ar TEXT)")
        self.conn.execute("""CREATE TABLE stock_products(
            id INTEGER PRIMARY KEY, category TEXT DEFAULT '', category_id INTEGER)""")
        self.conn.execute("INSERT INTO company_settings VALUES('activity','restaurant')")
        self.conn.execute("INSERT INTO stock_categories VALUES(1,'MISC','متنوع')")
        self.conn.execute("INSERT INTO stock_products VALUES(1,'',1)")
        self.assertEqual(resolve_product_account_code(self.conn, 1, "inventory", "1140"), "1140")

    def test_custom_profile_mapping_editor_saves_valid_account_roles(self):
        seed_activity_chart(self.conn, "service")
        profile_id = create_activity_profile(
            self.conn, "logistics", "النقل والخدمات اللوجستية", "service"
        )
        profile = next(p for p in list_activity_profiles(self.conn) if p["id"] == profile_id)
        update_custom_activity_profile(
            self.conn, profile_id,
            json.dumps(profile["account_roles"]),
            json.dumps(profile["posting_accounts"]),
            json.dumps(profile["tax_roles"]),
        )
        saved = next(p for p in list_activity_profiles(self.conn) if p["id"] == profile_id)
        self.assertEqual(saved["posting_accounts"], profile["posting_accounts"])
        self.assertEqual(saved["tax_roles"], profile["tax_roles"])

    def test_custom_profile_mapping_editor_rejects_wrong_account_type(self):
        seed_activity_chart(self.conn, "service")
        profile_id = create_activity_profile(
            self.conn, "logistics", "النقل والخدمات اللوجستية", "service"
        )
        profile = next(p for p in list_activity_profiles(self.conn) if p["id"] == profile_id)
        invalid_posting = dict(profile["posting_accounts"])
        invalid_posting["revenue"] = invalid_posting["supplier_ap"]
        with self.assertRaisesRegex(ValueError, "لا يتوافق"):
            update_custom_activity_profile(
                self.conn, profile_id,
                json.dumps(profile["account_roles"]),
                json.dumps(invalid_posting),
                json.dumps(profile["tax_roles"]),
            )

    def test_builtin_profile_mappings_are_read_only(self):
        builtin = next(p for p in list_activity_profiles(self.conn) if p["is_builtin"])
        with self.assertRaisesRegex(ValueError, "الأساسية"):
            update_custom_activity_profile(
                self.conn, builtin["id"], "{}", "{}", "{}"
            )

    def test_sales_and_purchase_modules_resolve_custom_profile_mappings(self):
        from sales import _sales_account_codes, _sales_output_vat_account
        from purchases import _activity_posting_accounts, _activity_tax_roles
        from activity_profiles import active_posting_accounts
        seed_activity_chart(self.conn, "service")
        self._company_settings()
        create_activity_profile(
            self.conn, "logistics", "النقل والخدمات اللوجستية", "service"
        )
        self.conn.execute(
            "INSERT INTO company_settings(setting_key,setting_value) VALUES('activity','service')"
        )
        self.conn.execute(
            "INSERT INTO company_settings(setting_key,setting_value) VALUES('activity_profile_code','logistics')"
        )
        self.assertEqual(_sales_account_codes(self.conn, "service"), active_posting_accounts(self.conn))
        self.assertEqual(_activity_posting_accounts(self.conn), active_posting_accounts(self.conn))
        self.assertEqual(_activity_tax_roles(self.conn)["input_vat"], "1165")
        self.assertIsInstance(_sales_output_vat_account(self.conn, "service"), int)

    def test_management_dashboard_uses_custom_profile_account_roles(self):
        from management_dashboard import load_management_account_roles
        seed_activity_chart(self.conn, "service")
        self._company_settings()
        self.conn.execute("""CREATE TABLE management_account_roles(
            account_id INTEGER NOT NULL, role TEXT NOT NULL,
            PRIMARY KEY(account_id,role))""")
        create_activity_profile(
            self.conn, "logistics", "النقل والخدمات اللوجستية", "service"
        )
        self.conn.execute(
            "INSERT INTO company_settings(setting_key,setting_value) VALUES('activity','service')"
        )
        self.conn.execute(
            "INSERT INTO company_settings(setting_key,setting_value) VALUES('activity_profile_code','logistics')"
        )
        stale_cash_id = self.conn.execute("SELECT id FROM accounts WHERE code='1120'").fetchone()["id"]
        expected_cash_id = self.conn.execute("SELECT id FROM accounts WHERE code='1110'").fetchone()["id"]
        self.conn.execute(
            "INSERT INTO management_account_roles(account_id,role) VALUES(?, 'cash')",
            (stale_cash_id,),
        )
        roles = load_management_account_roles(self.conn)
        self.assertEqual(roles["cash"], {expected_cash_id})

    def test_legacy_company_without_profile_tables_uses_builtin_mappings(self):
        from activity_profiles import active_posting_accounts
        self._company_settings()
        self.conn.execute(
            "INSERT INTO company_settings(setting_key,setting_value) VALUES('activity','commercial')"
        )
        self.assertEqual(active_posting_accounts(self.conn)["ar"], "1130")

    def test_adding_activity_accounts_preserves_existing_account_identity(self):
        seed_activity_chart(self.conn, "commercial")
        before = tuple(self.conn.execute(
            "SELECT id,code,name_ar,account_type,parent_id,is_group FROM accounts WHERE code='1110'"
        ).fetchone())
        ensure_activity_chart_accounts(self.conn, "service")
        after = tuple(self.conn.execute(
            "SELECT id,code,name_ar,account_type,parent_id,is_group FROM accounts WHERE code='1110'"
        ).fetchone())
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
