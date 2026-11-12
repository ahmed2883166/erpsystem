import sqlite3
import unittest

from chart_templates import (
    ACTIVITY_ACCOUNT_ROLES,
    ACTIVITY_POSTING_ACCOUNTS,
    CHARTS,
    migrate_vat_accounts,
)


class ActivityAccountMappingTests(unittest.TestCase):
    def test_every_role_code_exists_as_a_detail_account_of_the_expected_type(self):
        for activity, roles in ACTIVITY_ACCOUNT_ROLES.items():
            chart = {row[0]: row for row in CHARTS[activity]}
            for role, codes in roles.items():
                expected_type = "liability" if role in {"payables", "current_liabilities"} else (
                    "expense" if role == "direct_cost" else "asset"
                )
                for code in codes:
                    with self.subTest(activity=activity, role=role, code=code):
                        self.assertIn(code, chart)
                        self.assertEqual(chart[code][2], expected_type)
                        self.assertEqual(chart[code][4], 0)

    def test_posting_accounts_match_each_activity_chart(self):
        for activity, mapping in ACTIVITY_POSTING_ACCOUNTS.items():
            chart = {row[0]: row for row in CHARTS[activity]}
            for key in ("ar", "revenue", "vat", "inventory", "cogs",
                        "purchase_inventory", "supplier_ap", "input_vat"):
                code = mapping[key]
                if code is None:
                    continue
                expected_type = {
                    "ar": "asset", "revenue": "revenue", "vat": "liability",
                    "inventory": "asset", "cogs": "expense",
                    "purchase_inventory": "asset", "supplier_ap": "liability",
                    "input_vat": "asset",
                }[key]
                with self.subTest(activity=activity, key=key, code=code):
                    self.assertIn(code, chart)
                    self.assertEqual(chart[code][2], expected_type)
                    self.assertEqual(chart[code][4], 0)

    def test_industrial_v1_uses_same_stock_control_for_purchases_and_sales(self):
        mapping = ACTIVITY_POSTING_ACCOUNTS["industrial"]
        self.assertEqual(mapping["purchase_inventory"], "1130")
        self.assertEqual(mapping["inventory"], "1130")
        self.assertEqual(mapping["vat"], "2130")

    def test_vat_control_code_matches_each_activity_chart(self):
        expected = {activity: "2130" for activity in CHARTS}
        self.assertEqual(
            {activity: mapping["vat"] for activity, mapping in ACTIVITY_POSTING_ACCOUNTS.items()},
            expected,
        )
        self.assertEqual(
            {mapping["input_vat"] for mapping in ACTIVITY_POSTING_ACCOUNTS.values()},
            {"1165"},
        )

    def test_legacy_vat_code_migration_preserves_account_ids_and_is_idempotent(self):
        legacy = {
            "service": ("ضرائب مستحقة", None),
            "medical": ("مقدمات مرضى", "2140"),
            "restaurant": ("إكراميات مستحقة للعاملين", "2140"),
            "industrial": ("قروض", "2210"),
        }
        for activity, (legacy_name, displaced_code) in legacy.items():
            with self.subTest(activity=activity):
                conn = sqlite3.connect(":memory:")
                conn.row_factory = sqlite3.Row
                conn.execute("""CREATE TABLE accounts(
                    id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, name_ar TEXT NOT NULL,
                    account_type TEXT NOT NULL, parent_id INTEGER, is_group INTEGER NOT NULL DEFAULT 0
                )""")
                conn.execute("INSERT INTO accounts(code,name_ar,account_type,is_group) VALUES('2000','الالتزامات','liability',1)")
                conn.execute("INSERT INTO accounts(code,name_ar,account_type,is_group) VALUES('1100','الأصول المتداولة','asset',1)")
                conn.execute("INSERT INTO accounts(code,name_ar,account_type,is_group) VALUES('2130',?,'liability',0)", (legacy_name,))
                legacy_2130_id = conn.execute("SELECT id FROM accounts WHERE code='2130'").fetchone()["id"]
                old_vat_id = None
                if activity != "service":
                    conn.execute("INSERT INTO accounts(code,name_ar,account_type,is_group) VALUES('2120','ضرائب مستحقة','liability',0)")
                    old_vat_id = conn.execute("SELECT id FROM accounts WHERE code='2120'").fetchone()["id"]
                conn.execute("CREATE TABLE journal_lines(account_id INTEGER)")
                conn.execute("INSERT INTO journal_lines(account_id) VALUES(?)", (legacy_2130_id,))
                if old_vat_id:
                    conn.execute("INSERT INTO journal_lines(account_id) VALUES(?)", (old_vat_id,))

                migrate_vat_accounts(conn, activity)
                vat = conn.execute("SELECT id,name_ar,account_type,is_group FROM accounts WHERE code='2130'").fetchone()
                input_vat = conn.execute("SELECT account_type,is_group FROM accounts WHERE code='1165'").fetchone()
                self.assertEqual(vat["name_ar"], "ضريبة القيمة المضافة - مخرجات")
                self.assertEqual((vat["account_type"], vat["is_group"]), ("liability", 0))
                self.assertEqual((input_vat["account_type"], input_vat["is_group"]), ("asset", 0))
                if activity == "service":
                    self.assertEqual(vat["id"], legacy_2130_id)
                else:
                    moved = conn.execute("SELECT id FROM accounts WHERE code=?", (displaced_code,)).fetchone()
                    self.assertEqual(moved["id"], legacy_2130_id)
                    self.assertEqual(vat["id"], old_vat_id)
                    self.assertEqual(
                        [r["account_id"] for r in conn.execute("SELECT account_id FROM journal_lines ORDER BY rowid")],
                        [legacy_2130_id, old_vat_id],
                    )
                migrate_vat_accounts(conn, activity)
                self.assertEqual(conn.execute("SELECT id FROM accounts WHERE code='2130'").fetchone()["id"], vat["id"])
                conn.close()



if __name__ == "__main__":
    unittest.main()
