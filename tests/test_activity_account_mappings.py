import sqlite3
import unittest

from chart_templates import (
    ACTIVITY_ACCOUNT_ROLES,
    ACTIVITY_POSTING_ACCOUNTS,
    ACTIVITY_TAX_ACCOUNT_ROLES,
    CHARTS,
    migrate_vat_accounts,
)


class ActivityAccountMappingTests(unittest.TestCase):
    def test_every_role_code_exists_as_a_detail_account_of_the_expected_type(self):
        for activity, roles in ACTIVITY_ACCOUNT_ROLES.items():
            chart = {row[0]: row for row in CHARTS[activity]}
            for role, codes in roles.items():
                expected_type = "liability" if role in {"payables", "current_liabilities", "customer_advances"} else (
                    "expense" if role == "direct_cost" else (
                        "equity" if role == "retained_earnings" else "asset"
                    )
                )
                for code in codes:
                    with self.subTest(activity=activity, role=role, code=code):
                        self.assertIn(code, chart)
                        self.assertEqual(chart[code][2], expected_type)
                        self.assertEqual(chart[code][4], 0)

    def test_customer_advances_use_one_canonical_account_for_new_postings(self):
        self.assertEqual(ACTIVITY_ACCOUNT_ROLES["commercial"]["customer_advances"], ("2160",))
        self.assertEqual(ACTIVITY_ACCOUNT_ROLES["service"]["customer_advances"], ("2160",))
        # Legacy accounts remain in current-liability reporting so their historical balances
        # are not hidden when new transactions move to the canonical control account.
        self.assertIn("2140", ACTIVITY_ACCOUNT_ROLES["commercial"]["current_liabilities"])
        self.assertIn("2120", ACTIVITY_ACCOUNT_ROLES["service"]["current_liabilities"])

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

    def test_common_chart_expansion_adds_financial_control_branches(self):
        from chart_templates import seed_activity_chart, ensure_activity_chart_accounts

        for activity in CHARTS:
            with self.subTest(activity=activity):
                conn = sqlite3.connect(":memory:")
                conn.row_factory = sqlite3.Row
                conn.execute("""CREATE TABLE accounts(
                    id INTEGER PRIMARY KEY, code TEXT UNIQUE, name_ar TEXT,
                    account_type TEXT, parent_id INTEGER, is_group INTEGER DEFAULT 0,
                    active INTEGER DEFAULT 1)""")
                seed_activity_chart(conn, activity)
                before_ids = {
                    row["code"]: row["id"]
                    for row in conn.execute("SELECT id,code FROM accounts")
                }
                ensure_activity_chart_accounts(conn, activity)
                ensure_activity_chart_accounts(conn, activity)
                expected = {
                    "1170": ("asset", 1), "1171": ("asset", 0),
                    "1185": ("asset", 0), "6990": ("expense", 0), "6991": ("expense", 0),
                    "1180": ("asset", 0), "1250": ("asset", 0),
                    "1260": ("asset", 1), "1261": ("asset", 0),
                    "2170": ("liability", 1), "2190": ("liability", 0),
                    "2200": ("liability", 1), "2231": ("liability", 0),
                    "4700": ("revenue", 1), "4710": ("revenue", 0),
                    "5500": ("expense", 1), "5510": ("expense", 0),
                    "6800": ("expense", 1), "6810": ("expense", 0),
                    "7240": ("expense", 1), "7241": ("expense", 0),
                    "1190": ("asset", 1), "1191": ("asset", 0),
                    "2191": ("liability", 0), "2192": ("liability", 0),
                    "2232": ("liability", 0), "5600": ("expense", 1),
                    "5610": ("expense", 0), "5650": ("expense", 0),
                }
                for code, (account_type, is_group) in expected.items():
                    row = conn.execute(
                        "SELECT account_type,is_group FROM accounts WHERE code=?",
                        (code,),
                    ).fetchone()
                    self.assertIsNotNone(row, code)
                    self.assertEqual((row["account_type"], row["is_group"]),
                                     (account_type, is_group), code)
                expected_parents = {
                    "1171": "1170", "1185": "1100", "6990": "6000", "6991": "6000",
                    "1261": "1260", "2171": "2170",
                    "2231": "2230", "4710": "4700", "5510": "5500",
                    "6810": "6800", "7241": "7240",
                    "1191": "1190", "5610": "5600", "5650": "5600",
                }
                for code, parent_code in expected_parents.items():
                    parent = conn.execute("""
                        SELECT p.code FROM accounts child
                        JOIN accounts p ON p.id=child.parent_id
                        WHERE child.code=?
                    """, (code,)).fetchone()
                    self.assertIsNotNone(parent, code)
                    self.assertEqual(parent["code"], parent_code, code)
                for code, old_id in before_ids.items():
                    self.assertEqual(
                        conn.execute("SELECT id FROM accounts WHERE code=?", (code,)).fetchone()["id"],
                        old_id,
                    )
                duplicates = conn.execute(
                    "SELECT code,COUNT(*) n FROM accounts GROUP BY code HAVING COUNT(*)>1"
                ).fetchall()
                self.assertEqual(duplicates, [])
                conn.close()

    def test_operational_mapping_codes_are_marked_immutable(self):
        from chart_templates import OPERATIONAL_ACCOUNT_CODES

        self.assertTrue({"1110", "1130", "1165", "2130", "2190", "1185", "6990", "6991"}.issubset(
            OPERATIONAL_ACCOUNT_CODES
        ))

    def test_industrial_v1_uses_same_stock_control_for_purchases_and_sales(self):
        mapping = ACTIVITY_POSTING_ACCOUNTS["industrial"]
        self.assertEqual(mapping["purchase_inventory"], "1130")
        self.assertEqual(mapping["inventory"], "1130")
        self.assertEqual(mapping["vat"], "2130")

    def test_industrial_v1_chart_has_no_manufacturing_accounts(self):
        chart = {row[0]: row for row in CHARTS["industrial"]}
        self.assertEqual(set(ACTIVITY_ACCOUNT_ROLES["industrial"]["inventory"]), {"1130"})
        self.assertEqual(ACTIVITY_POSTING_ACCOUNTS["industrial"]["purchase_inventory"], "1130")
        self.assertEqual(ACTIVITY_POSTING_ACCOUNTS["industrial"]["inventory"], "1130")
        self.assertEqual(ACTIVITY_POSTING_ACCOUNTS["industrial"]["cogs"], "5400")
        self.assertTrue({"1140", "1150", "1160"}.isdisjoint(chart))
        self.assertEqual(chart["5400"][1], "تكلفة البضاعة المباعة")
        self.assertFalse(any("إنتاج" in row[1] or "تصنيع" in row[1] for row in CHARTS["industrial"]))


    def test_tax_accounts_are_resolved_by_role_for_every_activity(self):
        self.assertEqual(set(ACTIVITY_TAX_ACCOUNT_ROLES), set(CHARTS))
        for activity, roles in ACTIVITY_TAX_ACCOUNT_ROLES.items():
            self.assertEqual(roles["output_vat"], "2130")
            self.assertEqual(roles["input_vat"], "1165")
            chart = {row[0]: row for row in CHARTS[activity]}
            self.assertEqual(chart[roles["output_vat"]][2], "liability")
            self.assertEqual(chart[roles["output_vat"]][4], 0)
            self.assertEqual(chart[roles["input_vat"]][2], "asset")
            self.assertEqual(chart[roles["input_vat"]][4], 0)

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
