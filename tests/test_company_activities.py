import sqlite3
import unittest

from company_activities import (
    initialize_company_activity_schema,
    enable_company_activity,
    disable_company_activity,
    list_company_activities,
    set_activity_posting_account,
    resolve_activity_profile,
    resolve_activity_posting_account,
)


class CompanyActivityPackTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""CREATE TABLE accounts(
            id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL,
            account_type TEXT NOT NULL, is_group INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1)""")
        self.conn.executemany(
            "INSERT INTO accounts(code,account_type) VALUES(?,?)",
            [("1110","asset"),("1130","asset"),("4100","revenue"),
             ("5100","expense"),("2110","liability"),("1165","asset")],
        )
        self.conn.execute("""CREATE TABLE company_settings(
            setting_key TEXT PRIMARY KEY, setting_value TEXT)""")
        self.conn.execute(
            "INSERT INTO company_settings VALUES('activity','commercial')"
        )
        initialize_company_activity_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_conflicting_second_activity_is_blocked_without_changing_legacy_setting(self):
        enable_company_activity(self.conn, "commercial", primary=True)
        with self.assertRaisesRegex(ValueError, "خريطة الحسابات الموحدة"):
            enable_company_activity(self.conn, "service")
        self.assertEqual(
            [x["activity_code"] for x in list_company_activities(self.conn)],
            ["commercial"],
        )
        self.assertEqual(
            self.conn.execute("SELECT setting_value FROM company_settings WHERE setting_key='activity'").fetchone()[0],
            "commercial",
        )

    def test_primary_is_unique_and_cannot_change_on_rejected_activity(self):
        enable_company_activity(self.conn, "commercial", primary=True)
        with self.assertRaisesRegex(ValueError, "خريطة الحسابات الموحدة"):
            enable_company_activity(self.conn, "service", primary=True)
        rows = list_company_activities(self.conn)
        self.assertEqual(sum(row["is_primary"] for row in rows), 1)
        self.assertEqual(next(row["activity_code"] for row in rows if row["is_primary"]), "commercial")

    def test_document_context_resolves_activity_specific_posting(self):
        enable_company_activity(self.conn, "service", primary=True)
        set_activity_posting_account(self.conn, "service", "revenue", "4100")
        self.assertEqual(resolve_activity_posting_account(self.conn, "service", "revenue"), "4100")
        self.assertEqual(resolve_activity_profile(self.conn, "service")["base_activity"], "service")

    def test_invalid_mapping_type_is_rejected(self):
        enable_company_activity(self.conn, "commercial", primary=True)
        with self.assertRaisesRegex(ValueError, "نوع الحساب"):
            set_activity_posting_account(self.conn, "commercial", "revenue", "1110")

    def test_disabled_pack_cannot_be_resolved_or_disabled_if_primary(self):
        enable_company_activity(self.conn, "commercial", primary=True)
        # Simulate a previously provisioned secondary pack; disable must preserve it.
        self.conn.execute(
            "INSERT INTO company_activity_packs(activity_code,profile_code,enabled,is_primary) VALUES('service','service',1,0)"
        )
        disable_company_activity(self.conn, "service")
        with self.assertRaisesRegex(ValueError, "غير مفعّل"):
            resolve_activity_profile(self.conn, "service")
        with self.assertRaisesRegex(ValueError, "النشاط الأساسي"):
            disable_company_activity(self.conn, "commercial")

    def test_legacy_document_without_activity_uses_existing_profile(self):
        profile = resolve_activity_profile(self.conn)
        self.assertEqual(profile["base_activity"], "commercial")


if __name__ == "__main__":
    unittest.main()
