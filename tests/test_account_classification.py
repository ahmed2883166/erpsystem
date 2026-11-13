import sqlite3
import unittest

from chart_templates import account_metadata_for, ensure_account_metadata


class AccountClassificationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""CREATE TABLE accounts(
            id INTEGER PRIMARY KEY, code TEXT UNIQUE, name_ar TEXT,
            account_type TEXT, parent_id INTEGER, is_group INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1
        )""")
        self.conn.executemany(
            "INSERT INTO accounts(code,name_ar,account_type) VALUES(?,?,?)",
            [
                ("1110", "الصندوق", "asset"),
                ("1180", "مخصص خسائر ائتمانية متوقعة - العملاء", "asset"),
                ("2130", "ضريبة القيمة المضافة - مخرجات", "liability"),
                ("2210", "قروض وتسهيلات طويلة الأجل", "liability"),
                ("4200", "مردودات ومسموحات المبيعات", "revenue"),
                ("5610", "تكاليف مقاولي الباطن والناقلين", "expense"),
            ],
        )

    def tearDown(self):
        self.conn.close()

    def test_metadata_classifies_normal_balances_and_statement_sections(self):
        cash = account_metadata_for("asset", "1110", "الصندوق")
        allowance = account_metadata_for(
            "asset", "1180", "مخصص خسائر ائتمانية متوقعة - العملاء"
        )
        vat = account_metadata_for(
            "liability", "2130", "ضريبة القيمة المضافة - مخرجات"
        )
        loan = account_metadata_for(
            "liability", "2210", "قروض وتسهيلات طويلة الأجل"
        )
        returns = account_metadata_for(
            "revenue", "4200", "مردودات ومسموحات المبيعات"
        )
        project_cost = account_metadata_for(
            "expense", "5610", "تكاليف مقاولي الباطن والناقلين"
        )

        self.assertEqual(
            (cash["normal_balance"], cash["statement_section"],
             cash["cash_flow_category"], cash["account_subtype"],
             cash["is_control_account"]),
            ("debit", "current_assets", "cash_equivalents", "cash", 1),
        )
        self.assertEqual(allowance["normal_balance"], "credit")
        self.assertEqual(allowance["statement_section"], "current_assets")
        self.assertEqual(vat["normal_balance"], "credit")
        self.assertEqual(vat["statement_section"], "current_liabilities")
        self.assertEqual(vat["account_subtype"], "tax_control")
        self.assertEqual(vat["is_control_account"], 1)
        self.assertEqual(loan["statement_section"], "non_current_liabilities")
        self.assertEqual(loan["account_subtype"], "loan")
        self.assertEqual(returns["normal_balance"], "debit")
        self.assertEqual(returns["account_subtype"], "contra_revenue")
        self.assertEqual(project_cost["statement_section"], "direct_costs")
        self.assertEqual(project_cost["normal_balance"], "debit")

    def test_contract_advances_retention_and_warranty_accounts_get_specific_subtypes(self):
        self.assertEqual(
            account_metadata_for("liability", "2160", "دفعات مقدمة من العملاء")["account_subtype"],
            "customer_advance",
        )
        self.assertEqual(
            account_metadata_for("asset", "1193", "دفعات مقدمة لمقاولي الباطن والناقلين")["account_subtype"],
            "supplier_or_carrier_advance",
        )
        self.assertEqual(
            account_metadata_for("liability", "2232", "مخصص ضمان وصيانة تعاقدية")["account_subtype"],
            "warranty_provision",
        )

    def test_schema_migration_is_additive_idempotent_and_preserves_account_ids(self):
        before = {
            row["code"]: row["id"]
            for row in self.conn.execute("SELECT id,code FROM accounts")
        }
        ensure_account_metadata(self.conn)
        ensure_account_metadata(self.conn)

        columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(accounts)")
        }
        self.assertTrue({
            "normal_balance", "statement_section", "cash_flow_category",
            "account_subtype", "is_control_account",
        }.issubset(columns))
        for code, account_id in before.items():
            row = self.conn.execute(
                """SELECT id,normal_balance,statement_section,is_control_account
                   FROM accounts WHERE code=?""",
                (code,),
            ).fetchone()
            self.assertEqual(row["id"], account_id)
            self.assertIsNotNone(row["statement_section"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0],
            len(before),
        )


if __name__ == "__main__":
    unittest.main()
