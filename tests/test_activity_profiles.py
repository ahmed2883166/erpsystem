import sqlite3
import unittest

from activity_profiles import (
    initialize_activity_profiles_schema, list_activity_profiles,
    create_activity_profile, set_activity_profile_active,
    validate_activity_profile,
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

    def test_seeds_all_builtin_profiles_idempotently(self):
        initialize_activity_profiles_schema(self.conn)
        profiles = list_activity_profiles(self.conn)
        self.assertEqual({p["code"] for p in profiles},
                         {"commercial", "service", "medical", "restaurant", "industrial"})

    def test_custom_profile_inherits_base_configuration(self):
        profile_id = create_activity_profile(
            self.conn, "logistics", "النقل والخدمات اللوجستية", "service", "ملف مخصص"
        )
        profile = next(p for p in list_activity_profiles(self.conn) if p["id"] == profile_id)
        self.assertEqual(profile["base_activity"], "service")
        self.assertEqual(profile["account_roles"]["cash"], ("1110",))
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
