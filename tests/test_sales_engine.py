import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from decimal import Decimal

import analytic_dimensions
import sales
from unittest.mock import patch
import party_reports
import quotations
import stock


class SalesIntegrationTests(unittest.TestCase):
    def test_credit_note_cancellation_locks_invoice_before_credit_note(self):
        calls = []

        class Cursor:
            def __init__(self, row):
                self.row = row

            def fetchone(self):
                return self.row

        def fake_execute(_conn, sql, params=()):
            calls.append((sql, tuple(params)))
            if "SELECT invoice_id FROM sales_credit_notes" in sql:
                return Cursor({"invoice_id": 44})
            if "FROM sales_invoices WHERE id=?" in sql:
                return Cursor({"id": 44})
            if "FROM sales_credit_notes WHERE id=?" in sql:
                return Cursor({"id": 55, "invoice_id": 44})
            raise AssertionError("Unexpected SQL: " + sql)

        with patch("sales.database_dialect", return_value="postgresql"), \
             patch("sales.execute_compat", side_effect=fake_execute):
            invoice, note = sales._sales_credit_note_and_invoice_for_update(self.db, 55)

        self.assertEqual(invoice["id"], 44)
        self.assertEqual(note["id"], 55)
        self.assertIn("FROM sales_invoices WHERE id=? FOR UPDATE", calls[1][0])
        self.assertIn("FROM sales_credit_notes WHERE id=? AND status='posted' FOR UPDATE", calls[2][0])
        self.assertEqual([params for _, params in calls], [(55,), (44,), (55,)])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "nexus.db"
        sales.DB_PATH = path
        stock.DB_PATH = path
        stock.initialize_stock_schema()
        sales.init_sales_schema()
        self.db = sales.db()
        self.db.executescript("""
        CREATE TABLE accounts(
          id INTEGER PRIMARY KEY, code TEXT UNIQUE, name_ar TEXT, account_type TEXT,
          parent_id INTEGER, is_group INTEGER DEFAULT 0, active INTEGER DEFAULT 1
        );
        CREATE TABLE journal_entries(
          id INTEGER PRIMARY KEY, entry_date TEXT, reference TEXT, description TEXT,
          status TEXT, created_by INTEGER, created_at TEXT
        );
        CREATE TABLE journal_lines(
          id INTEGER PRIMARY KEY, entry_id INTEGER, account_id INTEGER,
          debit TEXT DEFAULT 0, credit TEXT DEFAULT 0, memo TEXT
        );
        CREATE TABLE company_settings(setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL);
        CREATE TABLE tax_rates(id INTEGER PRIMARY KEY, code TEXT, name_ar TEXT, rate REAL, active INTEGER DEFAULT 1);
        INSERT INTO tax_rates(code,name_ar,rate,active) VALUES('VAT','ضريبة القيمة المضافة',14,1);
        INSERT INTO company_settings VALUES('activity','commercial');
        INSERT INTO company_settings VALUES('inventory_cost_method','average');
        """)
        for code, name, typ in [
            ("1110", "الصندوق", "asset"), ("1120", "البنك", "asset"), ("1130", "العملاء", "asset"), ("2160", "دفعات مقدمة من العملاء", "liability"),
            ("1150", "المخزون", "asset"), ("2130", "ضريبة مخرجات", "liability"),
            ("4100", "المبيعات", "revenue"), ("4200", "مردودات ومسموحات المبيعات", "revenue"),
            ("5100", "تكلفة المبيعات", "expense")
        ]:
            self.db.execute("INSERT OR IGNORE INTO accounts(code,name_ar,account_type) VALUES(?,?,?)", (code,name,typ))
        self.db.execute("INSERT INTO customers(code,name) VALUES('C1','عميل اختبار')")
        self.customer_id = self.db.execute("SELECT id FROM customers").fetchone()["id"]
        self.db.execute("INSERT INTO stock_products(sku,name_ar) VALUES('P1','صنف اختبار')")
        self.product_id = self.db.execute("SELECT id FROM stock_products").fetchone()["id"]
        self.db.execute("INSERT INTO stock_warehouses(code,name_ar) VALUES('W1','المخزن الرئيسي')")
        self.warehouse_id = self.db.execute("SELECT id FROM stock_warehouses").fetchone()["id"]
        self.db.execute("""INSERT INTO stock_movements(movement_no,movement_type,product_id,target_warehouse_id,
            quantity,unit_cost,total_cost,movement_date) VALUES('OPEN','receipt',?,?, '5','10','50','2026-11-10')""",
            (self.product_id,self.warehouse_id))
        opening_move = self.db.execute("SELECT id FROM stock_movements WHERE movement_no='OPEN'").fetchone()["id"]
        self.db.execute("""INSERT INTO stock_valuation_layers(product_id,warehouse_id,source_movement_id,
            qty_in,qty_remaining,unit_cost) VALUES(?,?,?,'5','5','10')""",
            (self.product_id,self.warehouse_id,opening_move))
        self.db.commit()
        analytic_dimensions.initialize_analytic_dimensions_schema(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_postgresql_credit_note_operations_lock_invoice_and_note_rows(self):
        calls = []

        class Cursor:
            def __init__(self, row):
                self.row = row

            def fetchone(self):
                return self.row

        def fake_execute(_conn, sql, params=()):
            calls.append((sql, tuple(params)))
            return Cursor({"id": params[0]})

        with patch("sales.database_dialect", return_value="postgresql"), \
             patch("sales.execute_compat", side_effect=fake_execute):
            customer = sales._customer_for_update(self.db, 33)
            invoice = sales._sales_invoice_for_update(self.db, 44, posted_only=True)
            note = sales._sales_credit_note_for_update(self.db, 55)

        self.assertEqual(customer["id"], 33)
        self.assertEqual(invoice["id"], 44)
        self.assertEqual(note["id"], 55)
        self.assertIn("active=1 FOR UPDATE", calls[0][0])
        self.assertIn("status='posted' FOR UPDATE", calls[1][0])
        self.assertIn("status='posted' FOR UPDATE", calls[2][0])
        self.assertEqual(calls[0][1], (33,))
        self.assertEqual(calls[1][1], (44,))
        self.assertEqual(calls[2][1], (55,))

        with patch("sales.database_dialect", return_value="sqlite"), \
             patch("sales.execute_compat", side_effect=fake_execute):
            sales._customer_for_update(self.db, 33)
            sales._sales_invoice_for_update(self.db, 44, posted_only=True)
            sales._sales_credit_note_for_update(self.db, 55)
        self.assertNotIn("FOR UPDATE", calls[3][0])
        self.assertNotIn("FOR UPDATE", calls[4][0])
        self.assertNotIn("FOR UPDATE", calls[5][0])

    def test_invoice_draft_and_approval_record_do_not_post_stock_or_journal(self):
        payload = {
            "customer_id": self.customer_id,
            "invoice_date": "2026-11-10",
            "due_date": "2026-11-30",
            "warehouse_id": self.warehouse_id,
            "items": [{
                "product_id": self.product_id, "quantity": "1", "unit_price": "20",
                "description": "صنف اختبار", "uom_id": "", "tax_rate": "14",
                "tax_treatment": "standard",
            }],
            "tax_rate": "14", "dimensions": {}, "sales_channel": "default",
            "discount_override": {
                "discount_type": "none", "discount_value": "0", "discount_amount": "0"
            },
        }
        workflow_id, workflow_no = sales.create_invoice_workflow(
            self.db, payload, "pending_approval", 11, Decimal("20"),
            Decimal("0"), Decimal("0")
        )
        workflow = self.db.execute(
            "SELECT * FROM sales_invoice_workflows WHERE id=?", (workflow_id,)
        ).fetchone()
        self.assertEqual(workflow["workflow_no"], workflow_no)
        self.assertEqual(workflow["status"], "pending_approval")
        self.assertIsNone(workflow["posted_invoice_id"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM sales_invoices").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0], 0)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM stock_movements WHERE movement_type='issue'"
        ).fetchone()[0], 0)
        remaining = self.db.execute(
            "SELECT qty_remaining FROM stock_valuation_layers"
        ).fetchone()["qty_remaining"]
        self.assertEqual(Decimal(remaining), Decimal("5"))

    def test_draft_workflow_can_be_edited_without_posting_stock_or_journal(self):
        payload = {
            "customer_id": self.customer_id, "invoice_date": "2026-11-10",
            "due_date": "2026-11-30", "warehouse_id": self.warehouse_id,
            "items": [{"product_id": self.product_id, "quantity": "1", "unit_price": "20",
                       "description": "بند قبل التعديل", "uom_id": "",
                       "tax_rate": "14", "tax_treatment": "standard"}],
            "tax_rate": "14", "dimensions": {}, "sales_channel": "default",
            "discount_override": {"discount_type": "none", "discount_value": "0",
                                  "discount_amount": "0"},
        }
        workflow_id, _ = sales.create_invoice_workflow(
            self.db, payload, "draft", 11, Decimal("20"), Decimal("0"), Decimal("0")
        )
        payload["invoice_date"] = "2026-11-12"
        payload["due_date"] = "2026-12-01"
        payload["items"][0]["description"] = "بند بعد التعديل"
        payload["items"][0]["unit_price"] = "25"
        sales.update_invoice_workflow(
            self.db, workflow_id, payload, 11, Decimal("25"), Decimal("0"),
            Decimal("0")
        )
        saved = self.db.execute(
            "SELECT status,invoice_date,due_date,gross_subtotal,payload_json,updated_by "
            "FROM sales_invoice_workflows WHERE id=?", (workflow_id,)
        ).fetchone()
        saved_payload = json.loads(saved["payload_json"])
        self.assertEqual(saved["status"], "draft")
        self.assertEqual(saved["invoice_date"], "2026-11-12")
        self.assertEqual(saved["due_date"], "2026-12-01")
        self.assertEqual(Decimal(saved["gross_subtotal"]), Decimal("25.00"))
        self.assertEqual(saved_payload["items"][0]["description"], "بند بعد التعديل")
        self.assertEqual(saved["updated_by"], 11)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM sales_invoices").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0], 0)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM stock_movements WHERE movement_type='issue'"
        ).fetchone()[0], 0)
        remaining = self.db.execute(
            "SELECT qty_remaining FROM stock_valuation_layers"
        ).fetchone()["qty_remaining"]
        self.assertEqual(Decimal(remaining), Decimal("5"))

    def test_draft_workflow_cannot_be_edited_by_another_non_admin_user(self):
        payload = {
            "customer_id": self.customer_id, "invoice_date": "2026-11-10",
            "due_date": "2026-11-30", "warehouse_id": self.warehouse_id,
            "items": [{"product_id": self.product_id, "quantity": "1", "unit_price": "20",
                       "description": "بند", "uom_id": "", "tax_rate": "14",
                       "tax_treatment": "standard"}],
        }
        workflow_id, _ = sales.create_invoice_workflow(
            self.db, payload, "draft", 11, Decimal("20"), Decimal("0"), Decimal("0")
        )
        with self.assertRaisesRegex(ValueError, "المسودات التي أنشأتها"):
            sales.update_invoice_workflow(
                self.db, workflow_id, payload, 12, Decimal("20"), Decimal("0"),
                Decimal("0"), is_admin=False
            )
        self.assertEqual(self.db.execute(
            "SELECT status,created_by FROM sales_invoice_workflows WHERE id=?", (workflow_id,)
        ).fetchone()[:], ("draft", 11))

    def test_submitted_invoice_workflow_cannot_be_edited(self):
        payload = {
            "customer_id": self.customer_id, "invoice_date": "2026-11-10",
            "due_date": "2026-11-30", "warehouse_id": self.warehouse_id,
            "items": [{"product_id": self.product_id, "quantity": "1", "unit_price": "20",
                       "description": "بند", "uom_id": "", "tax_rate": "14",
                       "tax_treatment": "standard"}],
        }
        workflow_id, _ = sales.create_invoice_workflow(
            self.db, payload, "pending_approval", 11, Decimal("20"), Decimal("0"), Decimal("0")
        )
        with self.assertRaisesRegex(ValueError, "مسودة غير مرسلة"):
            sales.update_invoice_workflow(
                self.db, workflow_id, payload, 11, Decimal("20"), Decimal("0"),
                Decimal("0")
            )

    def test_approved_invoice_workflow_can_be_posted_once_with_saved_payload(self):
        payload = {
            "customer_id": self.customer_id,
            "invoice_date": "2026-11-10",
            "due_date": "2026-11-30",
            "warehouse_id": self.warehouse_id,
            "items": [{
                "product_id": self.product_id, "quantity": "1", "unit_price": "20",
                "description": "صنف اختبار", "uom_id": "", "tax_rate": "14",
                "tax_treatment": "standard",
            }],
            "tax_rate": "14", "dimensions": {}, "sales_channel": "default",
            "discount_override": {
                "discount_type": "none", "discount_value": "0", "discount_amount": "0"
            },
        }
        workflow_id, _ = sales.create_invoice_workflow(
            self.db, payload, "pending_approval", 11, Decimal("20"),
            Decimal("0"), Decimal("0")
        )
        self.db.execute("""UPDATE sales_invoice_workflows SET status='approved',
            approved_by=12 WHERE id=?""", (workflow_id,))
        self.db.commit()
        self.db.execute("BEGIN IMMEDIATE")
        workflow = self.db.execute(
            "SELECT * FROM sales_invoice_workflows WHERE id=? AND status='approved'",
            (workflow_id,)
        ).fetchone()
        saved = json.loads(workflow["payload_json"])
        result = sales.post_invoice(
            self.db, saved["customer_id"], saved["invoice_date"],
            saved["warehouse_id"], saved["items"], saved["tax_rate"], 12,
            dimensions=saved["dimensions"],
            discount_override=saved["discount_override"],
            sales_channel=saved["sales_channel"],
        )
        self.db.execute("UPDATE sales_invoices SET due_date=? WHERE id=?",
                        (saved["due_date"], result["invoice_id"]))
        self.db.execute("""UPDATE sales_invoice_workflows SET status='posted',
            posted_invoice_id=?,posted_at=CURRENT_TIMESTAMP WHERE id=? AND status='approved'""",
            (result["invoice_id"], workflow_id))
        self.db.commit()
        final = self.db.execute(
            "SELECT status,posted_invoice_id FROM sales_invoice_workflows WHERE id=?",
            (workflow_id,)
        ).fetchone()
        self.assertEqual(final["status"], "posted")
        self.assertEqual(final["posted_invoice_id"], result["invoice_id"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM sales_invoices").fetchone()[0], 1)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM stock_movements WHERE movement_type='issue'"
        ).fetchone()[0], 1)


    def test_custom_activity_profile_controls_invoice_account_posting(self):
        from activity_profiles import (
            initialize_activity_profiles_schema, create_activity_profile,
            list_activity_profiles, update_custom_activity_profile,
        )
        from chart_templates import ensure_activity_chart_accounts
        ensure_activity_chart_accounts(self.db, "commercial")
        self.db.execute(
            "INSERT INTO accounts(code,name_ar,account_type) VALUES('1131','عملاء الخدمات','asset')"
        )
        self.db.execute(
            "INSERT INTO accounts(code,name_ar,account_type) VALUES('4101','إيرادات الخدمات','revenue')"
        )
        initialize_activity_profiles_schema(self.db)
        profile_id = create_activity_profile(
            self.db, "sales_custom", "ملف مبيعات مخصص", "commercial"
        )
        profile = next(p for p in list_activity_profiles(self.db) if p["id"] == profile_id)
        posting = dict(profile["posting_accounts"])
        posting["ar"] = "1131"
        posting["revenue"] = "4101"
        update_custom_activity_profile(
            self.db, profile_id, json.dumps(profile["account_roles"]),
            json.dumps(posting), json.dumps(profile["tax_roles"]),
        )
        self.db.execute(
            "INSERT INTO company_settings(setting_key,setting_value) VALUES('activity_profile_code','sales_custom')"
        )
        self.db.commit()
        self.db.execute("BEGIN IMMEDIATE")
        invoice = sales.post_invoice(
            self.db, self.customer_id, "2026-11-10", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "1", "unit_price": "20"}], "14", 1
        )
        self.db.commit()
        posted_codes = {
            row["code"] for row in self.db.execute(
                """SELECT DISTINCT a.code FROM journal_lines jl
                   JOIN accounts a ON a.id=jl.account_id
                   WHERE jl.entry_id IN (?,?)""",
                (invoice["journal_entry_id"], invoice["cogs_journal_entry_id"]),
            )
        }
        self.assertIn("1131", posted_codes)
        self.assertIn("4101", posted_codes)
        self.assertNotIn("1130", posted_codes)

    def test_restaurant_invoice_posts_category_specific_inventory_and_cogs(self):
        from activity_profiles import initialize_activity_profiles_schema

        self.db.execute("UPDATE company_settings SET setting_value='restaurant' WHERE setting_key='activity'")
        initialize_activity_profiles_schema(self.db)
        extra_accounts = [
            ("1140", "مخزون الغذاء", "asset"), ("1160", "مخزون التعبئة", "asset"),
            ("4300", "مبيعات توصيل", "revenue"),
            ("4400", "مبيعات التطبيقات", "revenue"),
            ("5200", "تكلفة المشروبات", "expense"), ("5400", "تكلفة التعبئة", "expense"),
        ]
        self.db.executemany(
            "INSERT INTO accounts(code,name_ar,account_type) VALUES(?,?,?)",
            extra_accounts,
        )
        categories = [
            ("FOOD", "غذاء", "1140", "5100"),
            ("BEVERAGE", "مشروبات", "1150", "5200"),
            ("PACKAGING", "تعبئة", "1160", "5400"),
        ]
        for code, name, inventory_code, cogs_code in categories:
            category_id = self.db.execute(
                "INSERT INTO stock_categories(code,name_ar) VALUES(?,?)", (code, name)
            ).lastrowid
            product_id = self.db.execute(
                "INSERT INTO stock_products(sku,name_ar,category_id) VALUES(?,?,?)",
                ("REST-" + code, name, category_id),
            ).lastrowid
            movement_id = self.db.execute(
                """INSERT INTO stock_movements(
                    movement_no,movement_type,product_id,target_warehouse_id,quantity,
                    unit_cost,total_cost,movement_date)
                    VALUES(?,'receipt',?,?,'2','10','20','2026-11-10')""",
                ("OPEN-" + code, product_id, self.warehouse_id),
            ).lastrowid
            self.db.execute(
                """INSERT INTO stock_valuation_layers(
                    product_id,warehouse_id,source_movement_id,qty_in,qty_remaining,unit_cost)
                    VALUES(?,?,?,'2','2','10')""",
                (product_id, self.warehouse_id, movement_id),
            )

            self.db.execute("BEGIN IMMEDIATE")
            invoice = sales.post_invoice(
                self.db, self.customer_id, "2026-11-14", self.warehouse_id,
                [{"product_id": product_id, "quantity": "1", "unit_price": "25"}],
                "0", 1, sales_channel="dine_in",
            )
            self.db.commit()

            posted = {
                row["code"]: (Decimal(row["debit"]), Decimal(row["credit"]))
                for row in self.db.execute(
                    """SELECT a.code,SUM(CAST(jl.debit AS NUMERIC)) debit,
                              SUM(CAST(jl.credit AS NUMERIC)) credit
                       FROM journal_lines jl JOIN accounts a ON a.id=jl.account_id
                       WHERE jl.entry_id=? GROUP BY a.code""",
                    (invoice["cogs_journal_entry_id"],),
                )
            }
            self.assertEqual(posted[cogs_code], (Decimal("10"), Decimal("0")))
            self.assertEqual(posted[inventory_code], (Decimal("0"), Decimal("10")))
            tagged_inventory = self.db.execute(
                """SELECT jl.stock_product_id,jl.stock_warehouse_id
                   FROM journal_lines jl JOIN accounts a ON a.id=jl.account_id
                   WHERE jl.entry_id=? AND a.code=?""",
                (invoice["cogs_journal_entry_id"], inventory_code),
            ).fetchone()
            self.assertEqual(tagged_inventory["stock_product_id"], product_id)
            self.assertEqual(tagged_inventory["stock_warehouse_id"], self.warehouse_id)
            self.assertEqual(invoice["total_cost"], Decimal("10.00"))
            movement = self.db.execute(
                "SELECT accounting_status,journal_entry_id FROM stock_movements WHERE movement_no LIKE ?",
                (invoice["invoice_no"] + "-%",),
            ).fetchone()
            self.assertEqual(movement["accounting_status"], "posted")
            self.assertEqual(movement["journal_entry_id"], invoice["cogs_journal_entry_id"])

    def test_industrial_v1_invoice_uses_only_1130_inventory_and_5400_cogs(self):
        from activity_profiles import initialize_activity_profiles_schema

        self.db.execute("UPDATE company_settings SET setting_value='industrial' WHERE setting_key='activity'")
        initialize_activity_profiles_schema(self.db)
        self.db.execute(
            "INSERT INTO accounts(code,name_ar,account_type) VALUES('5400','تكلفة البضاعة المباعة','expense')"
        )
        self.db.execute("BEGIN IMMEDIATE")
        invoice = sales.post_invoice(
            self.db, self.customer_id, "2026-11-14", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "1", "unit_price": "25"}],
            "0", 1,
        )
        self.db.commit()
        balances = {
            row["code"]: (Decimal(row["debit"]), Decimal(row["credit"]))
            for row in self.db.execute(
                """SELECT a.code,SUM(CAST(jl.debit AS NUMERIC)) debit,
                          SUM(CAST(jl.credit AS NUMERIC)) credit
                   FROM journal_lines jl JOIN accounts a ON a.id=jl.account_id
                   WHERE jl.entry_id=? GROUP BY a.code""",
                (invoice["cogs_journal_entry_id"],),
            )
        }
        self.assertEqual(balances["5400"], (Decimal("10"), Decimal("0")))
        self.assertEqual(balances["1130"], (Decimal("0"), Decimal("10")))
        self.assertNotIn("1140", balances)
        self.assertNotIn("1150", balances)
        self.assertEqual(invoice["total_cost"], Decimal("10.00"))

    def test_invoice_posts_revenue_tax_cogs_and_stock_issue(self):
        self.db.execute("BEGIN IMMEDIATE")
        with patch("sales.lock_stock_warehouses", wraps=sales.lock_stock_warehouses) as warehouse_lock:
            result = sales.post_invoice(self.db, self.customer_id, "2026-11-10", self.warehouse_id,
                [{"product_id": self.product_id, "quantity": "2", "unit_price": "20"}], "14", 1)
            warehouse_lock.assert_called_once_with(self.db, self.warehouse_id)
        self.db.commit()
        self.assertEqual(result["subtotal"], Decimal("40.00"))
        self.assertEqual(result["tax_amount"], Decimal("5.60"))
        self.assertEqual(result["total_amount"], Decimal("45.60"))
        self.assertEqual(result["total_cost"], Decimal("20.00"))
        remaining = self.db.execute("SELECT qty_remaining FROM stock_valuation_layers").fetchone()["qty_remaining"]
        self.assertEqual(Decimal(remaining), Decimal("3.0000"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0], 2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM stock_movements WHERE movement_type='issue'").fetchone()[0], 1)
        inventory_line = self.db.execute(
            """SELECT jl.stock_product_id,jl.stock_warehouse_id
               FROM journal_lines jl JOIN accounts a ON a.id=jl.account_id
               WHERE jl.entry_id=? AND a.code='1150'""",
            (result["cogs_journal_entry_id"],),
        ).fetchone()
        self.assertEqual(inventory_line["stock_product_id"], self.product_id)
        self.assertEqual(inventory_line["stock_warehouse_id"], self.warehouse_id)

    def test_restaurant_invoice_posts_to_selected_sales_channel(self):
        from chart_templates import ensure_activity_chart_accounts
        ensure_activity_chart_accounts(self.db, "restaurant")
        self.db.execute("UPDATE company_settings SET setting_value='restaurant' WHERE setting_key='activity'")
        self.db.execute(
            "INSERT INTO stock_categories(code,name_ar) VALUES('BEVERAGE','مشروبات')"
        )
        category_id = self.db.execute(
            "SELECT id FROM stock_categories WHERE code='BEVERAGE'"
        ).fetchone()["id"]
        self.db.execute(
            "UPDATE stock_products SET category_id=? WHERE id=?",
            (category_id, self.product_id),
        )
        self.db.commit()
        self.db.execute("BEGIN IMMEDIATE")
        invoice = sales.post_invoice(
            self.db, self.customer_id, "2026-11-10", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "1", "unit_price": "20"}],
            "14", 1, sales_channel="delivery_apps",
        )
        self.db.commit()
        revenue_code = self.db.execute(
            """SELECT a.code FROM journal_lines jl
               JOIN accounts a ON a.id=jl.account_id
               WHERE jl.entry_id=? AND jl.credit>0 AND a.account_type='revenue'""",
            (invoice["journal_entry_id"],),
        ).fetchone()["code"]
        stored = self.db.execute(
            "SELECT sales_channel,revenue_account_code FROM sales_invoices WHERE id=?",
            (invoice["invoice_id"],),
        ).fetchone()
        self.assertEqual(revenue_code, "4400")
        cogs_codes = {
            row["code"] for row in self.db.execute(
                """SELECT DISTINCT a.code FROM journal_lines jl
                   JOIN accounts a ON a.id=jl.account_id
                   WHERE jl.entry_id=? AND a.account_type='expense'""",
                (invoice["cogs_journal_entry_id"],),
            )
        }
        inventory_codes = {
            row["code"] for row in self.db.execute(
                """SELECT DISTINCT a.code FROM journal_lines jl
                   JOIN accounts a ON a.id=jl.account_id
                   WHERE jl.entry_id=? AND a.account_type='asset'""",
                (invoice["cogs_journal_entry_id"],),
            )
        }
        self.assertIn("5200", cogs_codes)
        self.assertIn("1150", inventory_codes)
        self.assertEqual(stored["sales_channel"], "delivery_apps")
        self.assertEqual(stored["revenue_account_code"], "4400")
        line_id = self.db.execute(
            "SELECT id FROM sales_invoice_lines WHERE invoice_id=?",
            (invoice["invoice_id"],),
        ).fetchone()["id"]
        self.db.execute("BEGIN IMMEDIATE")
        credit = sales.post_credit_note(
            self.db, invoice["invoice_id"], "2026-11-11", "مرتجع",
            [{"invoice_line_id": line_id, "quantity": "1"}], 1,
        )
        self.db.commit()
        reversed_revenue = self.db.execute(
            """SELECT a.code FROM journal_lines jl
               JOIN accounts a ON a.id=jl.account_id
               WHERE jl.entry_id=? AND jl.debit>0 AND a.account_type='revenue'""",
            (credit["journal_entry_id"],),
        ).fetchone()["code"]
        self.assertEqual(reversed_revenue, "4500")

    def test_mixed_line_tax_treatments(self):
        self.db.execute("BEGIN IMMEDIATE")
        result = sales.post_invoice(self.db, self.customer_id, "2026-11-10", self.warehouse_id, [
            {"product_id": self.product_id, "quantity": "1", "unit_price": "100",
             "tax_rate": "14", "tax_treatment": "standard"},
            {"product_id": self.product_id, "quantity": "1", "unit_price": "100",
             "tax_rate": "14", "tax_treatment": "exempt"},
        ], "14", 1)
        self.db.commit()
        self.assertEqual(result["subtotal"], Decimal("200.00"))
        self.assertEqual(result["tax_amount"], Decimal("14.00"))
        rows = self.db.execute(
            "SELECT tax_rate,tax_treatment,tax_amount FROM sales_invoice_lines WHERE invoice_id=? ORDER BY id",
            (result["invoice_id"],)
        ).fetchall()
        self.assertEqual([(x["tax_treatment"], Decimal(x["tax_amount"])) for x in rows],
                         [("standard", Decimal("14.00")), ("exempt", Decimal("0.00"))])

    def test_fully_exempt_invoice_ignores_unapproved_header_rate(self):
        self.db.execute("DELETE FROM accounts WHERE code='2130'")
        self.db.execute("BEGIN IMMEDIATE")
        result = sales.post_invoice(self.db, self.customer_id, "2026-11-10", self.warehouse_id, [
            {"product_id": self.product_id, "quantity": "1", "unit_price": "100",
             "tax_rate": "0", "tax_treatment": "exempt"},
        ], "7", 1)
        self.db.commit()
        self.assertEqual(result["tax_amount"], Decimal("0.00"))
        self.assertEqual(result["total_amount"], Decimal("100.00"))
        stored = self.db.execute(
            "SELECT tax_rate FROM sales_invoices WHERE id=?", (result["invoice_id"],)
        ).fetchone()["tax_rate"]
        self.assertEqual(Decimal(stored), Decimal("0.00"))
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM journal_lines WHERE account_id=(SELECT id FROM accounts WHERE code='2130')"
        ).fetchone()[0], 0)

    def test_unapproved_line_tax_rate_is_rejected(self):
        self.db.execute("BEGIN IMMEDIATE")
        with self.assertRaisesRegex(ValueError, "غير معتمدة"):
            sales.post_invoice(self.db, self.customer_id, "2026-11-10", self.warehouse_id, [
                {"product_id": self.product_id, "quantity": "1", "unit_price": "100",
                 "tax_rate": "7", "tax_treatment": "standard"},
            ], "14", 1)
        self.db.rollback()

    def test_header_rate_stores_effective_rate_for_mixed_lines(self):
        self.db.execute("BEGIN IMMEDIATE")
        result = sales.post_invoice(self.db, self.customer_id, "2026-11-10", self.warehouse_id, [
            {"product_id": self.product_id, "quantity": "1", "unit_price": "100",
             "tax_rate": "14", "tax_treatment": "standard"},
            {"product_id": self.product_id, "quantity": "1", "unit_price": "100",
             "tax_rate": "0", "tax_treatment": "zero_rated"},
        ], "14", 1)
        self.db.commit()
        stored = self.db.execute("SELECT tax_rate FROM sales_invoices WHERE id=?", (result["invoice_id"],)).fetchone()["tax_rate"]
        self.assertEqual(Decimal(stored), Decimal("7.00"))

    def test_credit_limit_is_enforced_by_posting_engine(self):
        self.db.execute("UPDATE customers SET credit_limit='10' WHERE id=?", (self.customer_id,))
        self.db.execute("BEGIN IMMEDIATE")
        with self.assertRaisesRegex(ValueError, "تم إيقاف الفاتورة"):
            sales.post_invoice(self.db, self.customer_id, "2026-11-10", self.warehouse_id,
                [{"product_id": self.product_id, "quantity": "1", "unit_price": "20"}], "0", 1)
        self.db.rollback()
        self.db.execute("BEGIN IMMEDIATE")
        result = sales.post_invoice(self.db, self.customer_id, "2026-11-10", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "1", "unit_price": "20"}], "0", 1,
            allow_credit_override=True)
        self.db.commit()
        self.assertTrue(result["credit_warning"])

    def test_customer_receipt_reduces_open_invoice_balance(self):
        self.db.execute("BEGIN IMMEDIATE")
        invoice = sales.post_invoice(self.db, self.customer_id, "2026-11-10", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "1", "unit_price": "100"}], "0", 1)
        self.db.execute("UPDATE sales_invoices SET paid_amount='0' WHERE id=?", (invoice["invoice_id"],))
        cash_id = self.db.execute("SELECT id FROM accounts WHERE code='1110'").fetchone()["id"]
        receipt = sales.post_customer_receipt(self.db, invoice["invoice_id"], "2026-11-10", "40", cash_id, 1)
        self.db.commit()
        paid = self.db.execute("SELECT paid_amount FROM sales_invoices WHERE id=?", (invoice["invoice_id"],)).fetchone()["paid_amount"]
        self.assertEqual(Decimal(paid), Decimal("40.00"))
        self.assertTrue(receipt["journal_entry_id"])

    def test_batch_customer_receipt_allocates_multiple_invoices_and_posts_one_balanced_journal(self):
        self.db.execute("BEGIN IMMEDIATE")
        first = sales.post_invoice(
            self.db, self.customer_id, "2026-11-10", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "1", "unit_price": "100"}], "0", 1,
        )
        second = sales.post_invoice(
            self.db, self.customer_id, "2026-11-11", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "1", "unit_price": "80"}], "0", 1,
        )
        cash_id = self.db.execute("SELECT id FROM accounts WHERE code='1110'").fetchone()["id"]
        receipt = sales.post_customer_receipt_batch(
            self.db, self.customer_id, "2026-11-12",
            [(first["invoice_id"], "30"), (second["invoice_id"], "40")],
            cash_id, 1, "BANK-DEP-001",
        )
        self.db.commit()

        self.assertEqual(receipt["total_amount"], Decimal("70.00"))
        self.assertEqual(receipt["invoice_count"], 2)
        paid = {
            row["id"]: Decimal(row["paid_amount"])
            for row in self.db.execute(
                "SELECT id,paid_amount FROM sales_invoices WHERE id IN (?,?)",
                (first["invoice_id"], second["invoice_id"]),
            )
        }
        self.assertEqual(paid[first["invoice_id"]], Decimal("30.00"))
        self.assertEqual(paid[second["invoice_id"]], Decimal("40.00"))
        allocations = self.db.execute(
            """SELECT invoice_id,amount FROM customer_receipt_allocations a
               JOIN customer_receipt_batches b ON b.id=a.batch_id
               WHERE b.receipt_no=? ORDER BY invoice_id""",
            (receipt["receipt_no"],),
        ).fetchall()
        self.assertEqual(sum((Decimal(row["amount"]) for row in allocations), Decimal("0")), Decimal("70.00"))
        journal = self.db.execute(
            """SELECT a.code,jl.debit,jl.credit FROM journal_lines jl
               JOIN accounts a ON a.id=jl.account_id WHERE jl.entry_id=?""",
            (receipt["journal_entry_id"],),
        ).fetchall()
        totals = {}
        for row in journal:
            debit, credit = totals.setdefault(row["code"], [Decimal("0"), Decimal("0")])
            totals[row["code"]][0] += Decimal(row["debit"])
            totals[row["code"]][1] += Decimal(row["credit"])
        self.assertEqual(totals["1110"], [Decimal("70.00"), Decimal("0.00")])
        self.assertEqual(totals["1130"], [Decimal("0.00"), Decimal("70.00")])

    def test_batch_customer_receipt_overallocation_is_rejected_without_partial_updates(self):
        self.db.execute("BEGIN IMMEDIATE")
        invoice = sales.post_invoice(
            self.db, self.customer_id, "2026-11-10", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "1", "unit_price": "100"}], "0", 1,
        )
        self.db.commit()
        cash_id = self.db.execute("SELECT id FROM accounts WHERE code='1110'").fetchone()["id"]
        with self.assertRaisesRegex(ValueError, "أكبر من المتبقي"):
            sales.post_customer_receipt_batch(
                self.db, self.customer_id, "2026-11-12",
                [(invoice["invoice_id"], "100.01")], cash_id, 1,
            )
        paid = self.db.execute(
            "SELECT paid_amount FROM sales_invoices WHERE id=?", (invoice["invoice_id"],)
        ).fetchone()["paid_amount"]
        self.assertEqual(Decimal(paid), Decimal("0"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM customer_receipt_batches").fetchone()[0], 0)

    def test_partial_credit_note_reverses_revenue_tax_cogs_and_restores_stock(self):
        self.db.execute("BEGIN IMMEDIATE")
        invoice = sales.post_invoice(
            self.db, self.customer_id, "2026-11-10", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "2", "unit_price": "20",
              "tax_rate": "14", "tax_treatment": "standard"}], "14", 1
        )
        line_id = self.db.execute(
            "SELECT id FROM sales_invoice_lines WHERE invoice_id=?", (invoice["invoice_id"],)
        ).fetchone()["id"]
        credit = sales.post_credit_note(
            self.db, invoice["invoice_id"], "2026-11-11", "مرتجع جزئي",
            [{"invoice_line_id": line_id, "quantity": "1"}], 1
        )
        self.db.commit()

        self.assertEqual(credit["subtotal"], Decimal("20.00"))
        self.assertEqual(credit["tax_amount"], Decimal("2.80"))
        self.assertEqual(credit["total_amount"], Decimal("22.80"))
        self.assertEqual(credit["total_cost"], Decimal("10.00"))
        invoice_row = self.db.execute(
            "SELECT credited_amount FROM sales_invoices WHERE id=?", (invoice["invoice_id"],)
        ).fetchone()
        self.assertEqual(Decimal(invoice_row["credited_amount"]), Decimal("22.80"))

        movement = self.db.execute(
            "SELECT movement_type,quantity,total_cost,accounting_status FROM stock_movements WHERE movement_no LIKE 'SCN-%'"
        ).fetchone()
        self.assertEqual(movement["movement_type"], "receipt")
        self.assertEqual(Decimal(movement["quantity"]), Decimal("1"))
        self.assertEqual(Decimal(movement["total_cost"]), Decimal("10.00"))
        self.assertEqual(movement["accounting_status"], "posted")
        returned_layer = self.db.execute(
            "SELECT value_remaining FROM stock_valuation_layers WHERE source_movement_id=("
            "SELECT id FROM stock_movements WHERE movement_no LIKE 'SCN-%')"
        ).fetchone()
        self.assertEqual(Decimal(returned_layer["value_remaining"]), Decimal("10.00"))

        inventory_qty = self.db.execute(
            "SELECT SUM(CAST(qty_remaining AS REAL)) FROM stock_valuation_layers"
        ).fetchone()[0]
        self.assertEqual(Decimal(str(inventory_qty)), Decimal("4.0"))
        credit_lines = self.db.execute(
            """SELECT a.code,jl.debit,jl.credit FROM journal_lines jl
               JOIN accounts a ON a.id=jl.account_id WHERE jl.entry_id=? ORDER BY a.code""",
            (credit["journal_entry_id"],),
        ).fetchall()
        amounts = {row["code"]: (Decimal(row["debit"]), Decimal(row["credit"])) for row in credit_lines}
        self.assertEqual(amounts["4200"], (Decimal("20.00"), Decimal("0")))
        self.assertEqual(amounts["2130"], (Decimal("2.80"), Decimal("0")))
        self.assertEqual(amounts["1130"], (Decimal("0"), Decimal("22.80")))

        with self.assertRaisesRegex(ValueError, "الكمية المطلوبة"):
            self.db.execute("BEGIN IMMEDIATE")
            sales.post_credit_note(
                self.db, invoice["invoice_id"], "2026-11-12", "محاولة تكرار المرتجع",
                [{"invoice_line_id": line_id, "quantity": "2"}], 1
            )
        self.db.rollback()

    def test_sales_invoice_collection_partial_return_and_refund_reconcile_gl_stock_and_trial_balance(self):
        # Bring the opening stock into the GL so the subledger-to-control
        # comparison includes the same opening balance on both sides.
        self.db.execute(
            "INSERT INTO accounts(code,name_ar,account_type) VALUES('3000','حقوق الملكية','equity')"
        )
        opening_entry_id = self.db.execute(
            """INSERT INTO journal_entries(entry_date,reference,description,status,created_by,created_at)
               VALUES('2026-11-10','OPEN-STOCK-E2E','إثبات رصيد مخزون أول المدة','posted',1,CURRENT_TIMESTAMP)"""
        ).lastrowid
        inventory_account_id = self.db.execute(
            "SELECT id FROM accounts WHERE code='1150'"
        ).fetchone()["id"]
        equity_account_id = self.db.execute(
            "SELECT id FROM accounts WHERE code='3000'"
        ).fetchone()["id"]
        self.db.executemany(
            "INSERT INTO journal_lines(entry_id,account_id,debit,credit,memo) VALUES(?,?,?,?,?)",
            [
                (opening_entry_id, inventory_account_id, "50.00", "0", "رصيد مخزون افتتاحي"),
                (opening_entry_id, equity_account_id, "0", "50.00", "حقوق ملكية"),
            ],
        )
        self.db.commit()

        self.db.execute("BEGIN IMMEDIATE")
        invoice = sales.post_invoice(
            self.db, self.customer_id, "2026-11-10", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "2", "unit_price": "20",
              "tax_rate": "14", "tax_treatment": "standard"}], "14", 1,
        )
        self.db.commit()

        cash_id = self.db.execute(
            "SELECT id FROM accounts WHERE code='1110'"
        ).fetchone()["id"]
        self.db.execute("BEGIN IMMEDIATE")
        receipt = sales.post_customer_receipt(
            self.db, invoice["invoice_id"], "2026-11-11", "45.60", cash_id, 1,
            "SALE-E2E-RECEIPT",
        )
        line_id = self.db.execute(
            "SELECT id FROM sales_invoice_lines WHERE invoice_id=?",
            (invoice["invoice_id"],),
        ).fetchone()["id"]
        credit = sales.post_credit_note(
            self.db, invoice["invoice_id"], "2026-11-12", "مرتجع جزئي بعد التحصيل",
            [{"invoice_line_id": line_id, "quantity": "1"}], 1,
        )
        refund = sales.post_customer_refund(
            self.db, credit["credit_note_id"], "2026-11-13", "22.80", cash_id, 1,
        )
        self.db.commit()

        self.assertTrue(receipt["receipt_no"])
        self.assertEqual(credit["total_amount"], Decimal("22.80"))
        self.assertEqual(credit["total_cost"], Decimal("10.00"))
        self.assertEqual(refund["amount"], Decimal("22.80"))

        invoice_row = self.db.execute(
            "SELECT total_amount,paid_amount,credited_amount,status FROM sales_invoices WHERE id=?",
            (invoice["invoice_id"],),
        ).fetchone()
        self.assertEqual(Decimal(invoice_row["total_amount"]), Decimal("45.60"))
        self.assertEqual(Decimal(invoice_row["paid_amount"]), Decimal("45.60"))
        self.assertEqual(Decimal(invoice_row["credited_amount"]), Decimal("22.80"))

        # After selling two units and returning one, four units worth 40 remain.
        stock_qty = sum((Decimal(row["qty_remaining"]) for row in self.db.execute(
            "SELECT qty_remaining FROM stock_valuation_layers WHERE product_id=?",
            (self.product_id,),
        )), Decimal("0"))
        stock_value = sum((Decimal(row["value_remaining"]) for row in self.db.execute(
            "SELECT value_remaining FROM stock_valuation_layers WHERE product_id=?",
            (self.product_id,),
        )), Decimal("0"))
        self.assertEqual(stock_qty, Decimal("4.0000"))
        self.assertEqual(stock_value, Decimal("40.00"))

        # Calculate posted GL by account after invoice, collection, credit note and refund.
        totals = {}
        for row in self.db.execute(
            """SELECT a.code,a.account_type,jl.debit,jl.credit
               FROM journal_lines jl
               JOIN journal_entries je ON je.id=jl.entry_id
               JOIN accounts a ON a.id=jl.account_id
               WHERE je.status='posted'"""
        ):
            debit, credit_amount = totals.get(
                row["code"], (Decimal("0"), Decimal("0"))
            )
            totals[row["code"]] = (
                debit + Decimal(str(row["debit"] or "0")),
                credit_amount + Decimal(str(row["credit"] or "0")),
            )

        # Customer control is fully cleared; net cash received is 22.80.
        self.assertEqual(totals["1130"], (Decimal("68.40"), Decimal("68.40")))
        self.assertEqual(totals["1110"], (Decimal("45.60"), Decimal("22.80")))
        # Net revenue 20, output VAT 2.80, net COGS 10, inventory 40.
        self.assertEqual(totals["4100"], (Decimal("0"), Decimal("40.00")))
        self.assertEqual(totals["4200"], (Decimal("20.00"), Decimal("0")))
        self.assertEqual(totals["2130"], (Decimal("2.80"), Decimal("5.60")))
        self.assertEqual(totals["5100"], (Decimal("20.00"), Decimal("10.00")))
        self.assertEqual(totals["1150"], (Decimal("60.00"), Decimal("20.00")))

        inventory_gl = totals["1150"][0] - totals["1150"][1]
        self.assertEqual(inventory_gl, stock_value)
        trial_balance_debit = sum((value[0] for value in totals.values()), Decimal("0"))
        trial_balance_credit = sum((value[1] for value in totals.values()), Decimal("0"))
        self.assertEqual(trial_balance_debit, trial_balance_credit)

        # Run the real financial-statement builders against the same posted
        # sale/collection/return/refund ledger, not a hand-built report fixture.
        from income_statement import build_income_statement
        from financial_planning import build_balance_sheet_snapshot

        self.db.execute("ALTER TABLE accounts ADD COLUMN statement_section TEXT")
        self.db.execute(
            "UPDATE accounts SET statement_section='direct_costs' WHERE code='5100'"
        )
        self.db.commit()
        income = build_income_statement(self.db, "2026-11-10", "2026-11-30")
        self.assertEqual(income["revenue_total"], Decimal("20.00"))
        self.assertEqual(income["cogs_total"], Decimal("10.00"))
        self.assertEqual(income["gross_profit"], Decimal("10.00"))
        self.assertEqual(income["operating_expense_total"], Decimal("0"))
        self.assertEqual(income["net_profit"], Decimal("10.00"))

        position = build_balance_sheet_snapshot(self.db, "2026-11-30")
        self.assertEqual(position["total_assets"], Decimal("62.80"))
        self.assertEqual(position["total_liabilities"], Decimal("2.80"))
        self.assertEqual(position["equity_before_profit"], Decimal("50.00"))
        self.assertEqual(position["net_profit"], Decimal("10.00"))
        self.assertEqual(position["total_equity"], Decimal("60.00"))
        self.assertEqual(position["balance_difference"], Decimal("0.00"))
        self.assertTrue(position["balanced"])

        unbalanced_entries = self.db.execute(
            """SELECT je.id FROM journal_entries je
               JOIN journal_lines jl ON jl.entry_id=je.id
               WHERE je.status='posted'
               GROUP BY je.id
               HAVING ABS(SUM(CAST(jl.debit AS NUMERIC))-
                          SUM(CAST(jl.credit AS NUMERIC)))>0.005"""
        ).fetchall()
        self.assertEqual(unbalanced_entries, [])

    def test_discounted_invoice_credit_notes_reverse_net_sales_and_tax_exactly(self):
        self.db.execute(
            "UPDATE customers SET discount_type='percent',discount_value='10' WHERE id=?",
            (self.customer_id,),
        )
        self.db.execute("BEGIN IMMEDIATE")
        invoice = sales.post_invoice(
            self.db, self.customer_id, "2026-11-10", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "2", "unit_price": "100"}], "14", 1,
        )
        self.assertEqual(invoice["subtotal"], Decimal("180.00"))
        self.assertEqual(invoice["tax_amount"], Decimal("25.20"))
        line_id = self.db.execute(
            "SELECT id FROM sales_invoice_lines WHERE invoice_id=?", (invoice["invoice_id"],)
        ).fetchone()["id"]

        first = sales.post_credit_note(
            self.db, invoice["invoice_id"], "2026-11-11", "مرتجع جزئي بعد الخصم",
            [{"invoice_line_id": line_id, "quantity": "1"}], 1,
        )
        second = sales.post_credit_note(
            self.db, invoice["invoice_id"], "2026-11-12", "استكمال المرتجع بعد الخصم",
            [{"invoice_line_id": line_id, "quantity": "1"}], 1,
        )
        self.db.commit()

        self.assertEqual(first["subtotal"] + second["subtotal"], Decimal("180.00"))
        self.assertEqual(first["tax_amount"] + second["tax_amount"], Decimal("25.20"))
        self.assertEqual(first["total_amount"] + second["total_amount"], Decimal("205.20"))
        self.assertEqual(first["total_cost"] + second["total_cost"], Decimal("20.00"))
        invoice_lines = self.db.execute(
            """SELECT a.code,jl.debit,jl.credit FROM journal_lines jl
               JOIN accounts a ON a.id=jl.account_id
               WHERE jl.entry_id IN (?,?)""",
            (first["journal_entry_id"], second["journal_entry_id"]),
        ).fetchall()
        credit_totals = {}
        for row in invoice_lines:
            debit, credit = credit_totals.get(row["code"], (Decimal("0"), Decimal("0")))
            credit_totals[row["code"]] = (
                debit + Decimal(str(row["debit"] or "0")),
                credit + Decimal(str(row["credit"] or "0")),
            )
        self.assertEqual(credit_totals["4200"][0], Decimal("180"))
        self.assertEqual(credit_totals["2130"][0], Decimal("25.2"))
        self.assertEqual(credit_totals["1130"][1], Decimal("205.2"))
        cogs_rows = self.db.execute(
            """SELECT a.code,jl.debit,jl.credit FROM journal_lines jl
               JOIN accounts a ON a.id=jl.account_id
               WHERE jl.entry_id IN (?,?)""",
            (first["cogs_journal_entry_id"], second["cogs_journal_entry_id"]),
        ).fetchall()
        cogs_totals = {}
        for row in cogs_rows:
            debit, credit = cogs_totals.get(row["code"], (Decimal("0"), Decimal("0")))
            cogs_totals[row["code"]] = (
                debit + Decimal(str(row["debit"] or "0")),
                credit + Decimal(str(row["credit"] or "0")),
            )
        self.assertEqual(cogs_totals["1150"], (Decimal("20.00"), Decimal("0")))
        self.assertEqual(cogs_totals["5100"], (Decimal("0"), Decimal("20.00")))
        self.assertEqual(
            Decimal(self.db.execute(
                "SELECT SUM(CAST(qty_remaining AS NUMERIC)) FROM stock_valuation_layers"
            ).fetchone()[0]),
            Decimal("5"),
        )
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM sales_credit_note_lines WHERE invoice_line_id=?",
                (line_id,),
            ).fetchone()[0],
            2,
        )

    def test_sales_return_restores_original_lot_and_expiry(self):
        self.db.execute(
            "INSERT INTO stock_products(sku,name_ar,track_lot,track_expiry) VALUES('LOT-1','صنف بتشغيلة',1,1)"
        )
        product_id = self.db.execute("SELECT id FROM stock_products WHERE sku='LOT-1'").fetchone()["id"]
        lot_id = stock.make_lot(
            self.db, self.db.execute("SELECT * FROM stock_products WHERE id=?", (product_id,)).fetchone(),
            "BATCH-2026-01", "2026-01-01", "2027-12-31"
        )
        self.db.execute(
            """INSERT INTO stock_movements(movement_no,movement_type,product_id,target_warehouse_id,
               lot_id,quantity,unit_cost,total_cost,movement_date)
               VALUES('LOT-OPEN','receipt',?,?,?,?,?,'20','2026-11-10')""",
            (product_id, self.warehouse_id, lot_id, "2", "10")
        )
        move_id = self.db.execute("SELECT id FROM stock_movements WHERE movement_no='LOT-OPEN'").fetchone()["id"]
        self.db.execute(
            """INSERT INTO stock_valuation_layers(product_id,warehouse_id,lot_id,source_movement_id,
               qty_in,qty_remaining,unit_cost,value_remaining) VALUES(?,?,?,?,?,?,?,?)""",
            (product_id, self.warehouse_id, lot_id, move_id, "2", "2", "10", "20")
        )
        self.db.commit()
        self.db.execute("BEGIN IMMEDIATE")
        invoice = sales.post_invoice(
            self.db, self.customer_id, "2026-11-10", self.warehouse_id,
            [{"product_id": product_id, "quantity": "2", "unit_price": "30"}], "0", 1
        )
        invoice_line_id = self.db.execute(
            "SELECT id FROM sales_invoice_lines WHERE invoice_id=?", (invoice["invoice_id"],)
        ).fetchone()["id"]
        self.assertEqual(self.db.execute(
            "SELECT lot_id FROM sales_invoice_line_lots WHERE invoice_line_id=?", (invoice_line_id,)
        ).fetchone()["lot_id"], lot_id)
        sales.post_credit_note(
            self.db, invoice["invoice_id"], "2026-11-11", "مرتجع من نفس التشغيلة",
            [{"invoice_line_id": invoice_line_id, "quantity": "1"}], 1
        )
        self.db.commit()
        returned = self.db.execute(
            """SELECT m.lot_id,l.expiry_date,m.quantity FROM stock_movements m
               JOIN stock_lots l ON l.id=m.lot_id WHERE m.issue_purpose='sales_return'"""
        ).fetchone()
        self.assertEqual(returned["lot_id"], lot_id)
        self.assertEqual(returned["expiry_date"], "2027-12-31")
        self.assertEqual(Decimal(returned["quantity"]), Decimal("1"))

    def test_credit_note_reduces_amount_available_for_customer_receipt(self):
        self.db.execute("BEGIN IMMEDIATE")
        invoice = sales.post_invoice(
            self.db, self.customer_id, "2026-11-10", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "1", "unit_price": "100"}], "0", 1
        )
        line_id = self.db.execute(
            "SELECT id FROM sales_invoice_lines WHERE invoice_id=?", (invoice["invoice_id"],)
        ).fetchone()["id"]
        sales.post_credit_note(
            self.db, invoice["invoice_id"], "2026-11-11", "تخفيض قبل التحصيل",
            [{"invoice_line_id": line_id, "quantity": "0.5"}], 1
        )
        cash_id = self.db.execute("SELECT id FROM accounts WHERE code='1110'").fetchone()["id"]
        with self.assertRaisesRegex(ValueError, "أكبر من الرصيد المستحق"):
            sales.post_customer_receipt(
                self.db, invoice["invoice_id"], "2026-11-12", "75", cash_id, 1
            )
        self.db.rollback()

    def test_fully_paid_invoice_credit_note_can_be_refunded(self):
        self.db.execute("BEGIN IMMEDIATE")
        invoice = sales.post_invoice(
            self.db, self.customer_id, "2026-11-10", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "1", "unit_price": "100"}], "0", 1
        )
        cash_id = self.db.execute("SELECT id FROM accounts WHERE code='1110'").fetchone()["id"]
        sales.post_customer_receipt(
            self.db, invoice["invoice_id"], "2026-11-10", "100", cash_id, 1
        )
        line_id = self.db.execute(
            "SELECT id FROM sales_invoice_lines WHERE invoice_id=?", (invoice["invoice_id"],)
        ).fetchone()["id"]
        credit = sales.post_credit_note(
            self.db, invoice["invoice_id"], "2026-11-11", "مرتجع بعد التحصيل الكامل",
            [{"invoice_line_id": line_id, "quantity": "0.5"}], 1
        )
        self.assertEqual(credit["total_amount"], Decimal("50.00"))
        note_id = credit["credit_note_id"]
        note = self.db.execute("SELECT refundable_amount FROM sales_credit_notes WHERE id=?", (note_id,)).fetchone()
        self.assertEqual(Decimal(note["refundable_amount"]), Decimal("50.00"))
        inventory_id = self.db.execute("SELECT id FROM accounts WHERE code='1150'").fetchone()["id"]
        with self.assertRaisesRegex(ValueError, "مربوطًا بملف النشاط"):
            sales.post_customer_refund(
                self.db, note_id, "2026-11-12", "50", inventory_id, 1, "INVALID-CASH"
            )
        refund = sales.post_customer_refund(
            self.db, note_id, "2026-11-12", "50", cash_id, 1, "BANK-REF-1"
        )
        self.db.commit()
        self.assertEqual(refund["amount"], Decimal("50.00"))
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM customer_refunds WHERE credit_note_id=?", (note_id,)
        ).fetchone()[0], 1)
        refund_lines = self.db.execute(
            "SELECT debit,credit FROM journal_lines WHERE entry_id=?", (refund["journal_entry_id"],)
        ).fetchall()
        self.assertEqual(sum((Decimal(x["debit"]) for x in refund_lines), Decimal("0")), Decimal("50.00"))
        self.assertEqual(sum((Decimal(x["credit"]) for x in refund_lines), Decimal("0")), Decimal("50.00"))

    def test_credit_note_cancellation_blocks_when_average_cost_revalues_return_layer(self):
        self.db.execute("BEGIN IMMEDIATE")
        invoice = sales.post_invoice(
            self.db, self.customer_id, "2026-11-10", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "2", "unit_price": "20"}], "0", 1
        )
        line_id = self.db.execute(
            "SELECT id FROM sales_invoice_lines WHERE invoice_id=?", (invoice["invoice_id"],)
        ).fetchone()["id"]
        credit = sales.post_credit_note(
            self.db, invoice["invoice_id"], "2026-11-11", "مرتجع لاختبار إعادة التقييم",
            [{"invoice_line_id": line_id, "quantity": "1"}], 1
        )
        returned_movement = self.db.execute(
            "SELECT stock_movement_id FROM sales_credit_note_lines WHERE credit_note_id=?",
            (credit["credit_note_id"],)
        ).fetchone()["stock_movement_id"]
        layer = self.db.execute(
            "SELECT l.id AS layer_id,l.qty_in,l.qty_remaining,m.total_cost "
            "FROM stock_valuation_layers l JOIN stock_movements m ON m.id=l.source_movement_id WHERE m.id=?",
            (returned_movement,)
        ).fetchone()
        self.assertEqual(Decimal(layer["qty_remaining"]), Decimal(layer["qty_in"]))
        self.assertEqual(Decimal(layer["total_cost"]), Decimal("10.00"))
        # A new receipt at a different cost followed by an average-cost issue
        # rebases the returned layer value without consuming its quantity.
        receipt_move = self.db.execute(
            """INSERT INTO stock_movements(movement_no,movement_type,product_id,target_warehouse_id,
               quantity,unit_cost,total_cost,movement_date)
               VALUES('AVG-REBASE-RECEIPT','receipt',?,?, '1','20','20','2026-11-11')""",
            (self.product_id, self.warehouse_id)
        ).lastrowid
        self.db.execute(
            """INSERT INTO stock_valuation_layers(product_id,warehouse_id,source_movement_id,
               qty_in,qty_remaining,unit_cost,value_remaining)
               VALUES(?,?,?,'1','1','20','20')""",
            (self.product_id, self.warehouse_id, receipt_move)
        )
        stock.consume_layers(
            self.db, self.product_id, self.warehouse_id, Decimal("1"), "average"
        )
        rebased_layer = self.db.execute(
            "SELECT qty_remaining,value_remaining FROM stock_valuation_layers WHERE id=?",
            (layer["layer_id"],)
        ).fetchone()
        self.assertEqual(Decimal(rebased_layer["qty_remaining"]), Decimal("1"))
        self.assertEqual(Decimal(rebased_layer["value_remaining"]), Decimal("12.00"))
        journal_count = self.db.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0]
        with self.assertRaisesRegex(ValueError, "القيمة الدفترية لطبقة المرتجع تغيرت"):
            sales.cancel_credit_note(
                self.db, credit["credit_note_id"], "2026-11-12",
                "اختبار منع إلغاء طبقة أعيد تقييمها", 1
            )
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0],
            journal_count,
        )
        self.assertEqual(
            self.db.execute(
                "SELECT status FROM sales_credit_notes WHERE id=?", (credit["credit_note_id"],)
            ).fetchone()["status"],
            "posted",
        )
        self.db.rollback()

    def test_credit_note_cancellation_posts_reversals_and_removes_returned_stock(self):
        self.db.execute("INSERT INTO accounts(code,name_ar,account_type) VALUES('3000','حقوق الملكية','equity')")
        inventory_id = self.db.execute("SELECT id FROM accounts WHERE code='1150'").fetchone()["id"]
        equity_id = self.db.execute("SELECT id FROM accounts WHERE code='3000'").fetchone()["id"]
        self.db.execute(
            """INSERT INTO journal_entries(entry_date,reference,description,status,created_by,created_at)
               VALUES('2026-01-01','OPEN-STOCK-SALES-CANCEL','رصيد مخزون افتتاحي','posted',1,'2026-01-01')"""
        )
        opening_id = self.db.execute("SELECT id FROM journal_entries WHERE reference='OPEN-STOCK-SALES-CANCEL'").fetchone()["id"]
        self.db.executemany(
            "INSERT INTO journal_lines(entry_id,account_id,debit,credit,memo) VALUES(?,?,?,?,?)",
            [(opening_id,inventory_id,'50','0','رصيد المخزون الافتتاحي'),
             (opening_id,equity_id,'0','50','حقوق الملكية الافتتاحية')]
        )
        self.db.execute("BEGIN IMMEDIATE")
        invoice = sales.post_invoice(
            self.db, self.customer_id, "2026-11-10", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "2", "unit_price": "20"}], "0", 1
        )
        line_id = self.db.execute(
            "SELECT id FROM sales_invoice_lines WHERE invoice_id=?", (invoice["invoice_id"],)
        ).fetchone()["id"]
        credit = sales.post_credit_note(
            self.db, invoice["invoice_id"], "2026-11-11", "مرتجع سيتم إلغاؤه",
            [{"invoice_line_id": line_id, "quantity": "1"}], 1
        )
        cancelled = sales.cancel_credit_note(
            self.db, credit["credit_note_id"], "2026-11-12", "إدخال المرتجع بالخطأ", 1
        )
        self.db.commit()
        note = self.db.execute(
            "SELECT status,cancel_journal_entry_id FROM sales_credit_notes WHERE id=?",
            (credit["credit_note_id"],)
        ).fetchone()
        self.assertEqual(note["status"], "cancelled")
        self.assertEqual(note["cancel_journal_entry_id"], cancelled["reversal_journal_id"])
        invoice_state = self.db.execute(
            "SELECT credited_amount FROM sales_invoices WHERE id=?", (invoice["invoice_id"],)
        ).fetchone()
        self.assertEqual(Decimal(invoice_state["credited_amount"]), Decimal("0"))
        layer = self.db.execute(
            "SELECT qty_remaining,value_remaining FROM stock_valuation_layers WHERE source_movement_id=("
            "SELECT stock_movement_id FROM sales_credit_note_lines WHERE credit_note_id=?)",
            (credit["credit_note_id"],)
        ).fetchone()
        self.assertEqual(Decimal(layer["qty_remaining"]), Decimal("0"))
        self.assertEqual(Decimal(layer["value_remaining"]), Decimal("0"))
        cancelled_movement = self.db.execute(
            """SELECT movement_type,accounting_status FROM stock_movements
               WHERE id=(SELECT stock_movement_id FROM sales_credit_note_lines WHERE credit_note_id=?)""",
            (credit["credit_note_id"],)
        ).fetchone()
        self.assertEqual(tuple(cancelled_movement), ("cancelled_receipt", "reversed"))
        self.assertEqual(
            stock.current_qty(self.db, self.product_id, self.warehouse_id),
            Decimal("3")
        )
        remaining_layer_value = sum(
            (Decimal(row["value_remaining"] or "0") for row in self.db.execute(
                "SELECT value_remaining FROM stock_valuation_layers WHERE product_id=?",
                (self.product_id,)
            ).fetchall()),
            Decimal("0")
        )
        inventory_gl = self.db.execute(
            """SELECT SUM(CAST(jl.debit AS NUMERIC)-CAST(jl.credit AS NUMERIC))
               FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
               WHERE jl.account_id=? AND je.status='posted'""",
            (inventory_id,)
        ).fetchone()[0]
        self.assertEqual(remaining_layer_value, Decimal("30.00"))
        self.assertEqual(Decimal(str(inventory_gl)), Decimal("30"))
        self.assertEqual(remaining_layer_value, Decimal(str(inventory_gl)))
        unbalanced = self.db.execute(
            """SELECT je.id FROM journal_entries je JOIN journal_lines jl ON jl.entry_id=je.id
               GROUP BY je.id
               HAVING ABS(SUM(CAST(jl.debit AS NUMERIC))-SUM(CAST(jl.credit AS NUMERIC)))>0.005"""
        ).fetchall()
        self.assertEqual(unbalanced, [])

    def test_insufficient_stock_rolls_back_invoice(self):
        self.db.execute("BEGIN IMMEDIATE")
        with self.assertRaisesRegex(ValueError, "الرصيد غير كافٍ"):
            sales.post_invoice(self.db, self.customer_id, "2026-11-10", self.warehouse_id,
                [{"product_id": self.product_id, "quantity": "99", "unit_price": "20"}], "0", 1)
        self.db.rollback()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM sales_invoices").fetchone()[0], 0)

    def test_customer_percentage_discount_reduces_taxable_sales_before_vat(self):
        self.db.execute("UPDATE customers SET discount_type='percent',discount_value='10' WHERE id=?", (self.customer_id,))
        self.db.execute("BEGIN IMMEDIATE")
        result = sales.post_invoice(self.db, self.customer_id, "2026-11-10", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "1", "unit_price": "100"}], "14", 1)
        self.db.commit()
        self.assertEqual(result["gross_subtotal"], Decimal("100.00"))
        self.assertEqual(result["discount_amount"], Decimal("10.00"))
        self.assertEqual(result["subtotal"], Decimal("90.00"))
        self.assertEqual(result["tax_amount"], Decimal("12.60"))
        self.assertEqual(result["total_amount"], Decimal("102.60"))
        line = self.db.execute("SELECT line_total,tax_amount FROM sales_invoice_lines WHERE invoice_id=?",
                               (result["invoice_id"],)).fetchone()
        self.assertEqual(Decimal(line["line_total"]), Decimal("90.00"))
        self.assertEqual(Decimal(line["tax_amount"]), Decimal("12.60"))

    def test_customer_fixed_discount_is_applied_and_snapshotted(self):
        self.db.execute("UPDATE customers SET discount_type='fixed',discount_value='15' WHERE id=?", (self.customer_id,))
        self.db.execute("BEGIN IMMEDIATE")
        result = sales.post_invoice(self.db, self.customer_id, "2026-11-10", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "1", "unit_price": "100"}], "0", 1)
        self.db.commit()
        self.assertEqual(result["discount_amount"], Decimal("15.00"))
        self.assertEqual(result["subtotal"], Decimal("85.00"))
        saved = self.db.execute("SELECT discount_type,discount_value,discount_amount FROM sales_invoices WHERE id=?",
                                (result["invoice_id"],)).fetchone()
        self.assertEqual((saved["discount_type"], saved["discount_value"], Decimal(saved["discount_amount"])),
                         ("fixed", "15.00", Decimal("15.00")))

    def test_customer_advance_receipt_allocation_and_refund_post_balanced_journals(self):
        self.db.execute("BEGIN IMMEDIATE")
        invoice = sales.post_invoice(
            self.db, self.customer_id, "2026-11-10", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "1", "unit_price": "100"}], "0", 1,
        )
        second_invoice = sales.post_invoice(
            self.db, self.customer_id, "2026-11-10", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "1", "unit_price": "50"}], "0", 1,
        )
        cash_id = self.db.execute("SELECT id FROM accounts WHERE code='1110'").fetchone()["id"]
        advance = sales.post_customer_advance_receipt(
            self.db, self.customer_id, "2026-11-11", "100", cash_id, 1, "ADV-001",
        )
        allocation = sales.allocate_customer_advance(
            self.db, advance["advance_receipt_id"],
            [(invoice["invoice_id"], "60"), (second_invoice["invoice_id"], "20")],
            "2026-11-12", 1,
        )
        bank_id = self.db.execute("SELECT id FROM accounts WHERE code='1120'").fetchone()["id"]
        refund = sales.post_customer_advance_refund(
            self.db, advance["advance_receipt_id"], "2026-11-13", "15", bank_id, 1, "REF-ADV-001",
        )
        self.db.commit()

        paid = self.db.execute(
            "SELECT paid_amount FROM sales_invoices WHERE id=?", (invoice["invoice_id"],)
        ).fetchone()["paid_amount"]
        self.assertEqual(Decimal(paid), Decimal("60.00"))
        second_paid = self.db.execute(
            "SELECT paid_amount FROM sales_invoices WHERE id=?", (second_invoice["invoice_id"],)
        ).fetchone()["paid_amount"]
        self.assertEqual(Decimal(second_paid), Decimal("20.00"))
        self.assertEqual(allocation["remaining_advance"], Decimal("20.00"))
        self.assertEqual(refund["remaining_advance"], Decimal("5.00"))

        expected = [
            (advance["journal_entry_id"], "1110", Decimal("100.00"), Decimal("0.00")),
            (advance["journal_entry_id"], "2160", Decimal("0.00"), Decimal("100.00")),
            (allocation["journal_entry_id"], "2160", Decimal("80.00"), Decimal("0.00")),
            (allocation["journal_entry_id"], "1130", Decimal("0.00"), Decimal("80.00")),
            (refund["journal_entry_id"], "2160", Decimal("15.00"), Decimal("0.00")),
            (refund["journal_entry_id"], "1120", Decimal("0.00"), Decimal("15.00")),
        ]
        actual_totals = {}
        for row in self.db.execute(
            """SELECT jl.entry_id,a.code,jl.debit,jl.credit
               FROM journal_lines jl JOIN accounts a ON a.id=jl.account_id
               WHERE jl.entry_id IN (?,?,?)""",
            (advance["journal_entry_id"], allocation["journal_entry_id"], refund["journal_entry_id"]),
        ):
            key = (row["entry_id"], row["code"])
            totals = actual_totals.setdefault(key, [Decimal("0"), Decimal("0")])
            totals[0] += Decimal(row["debit"])
            totals[1] += Decimal(row["credit"])
        expected_totals = {
            (entry_id, code): [debit, credit]
            for entry_id, code, debit, credit in expected
        }
        self.assertEqual(actual_totals, expected_totals)
        for entry_id in (advance["journal_entry_id"], allocation["journal_entry_id"], refund["journal_entry_id"]):
            totals = self.db.execute(
                "SELECT SUM(CAST(debit AS REAL)) debit,SUM(CAST(credit AS REAL)) credit FROM journal_lines WHERE entry_id=?",
                (entry_id,),
            ).fetchone()
            self.assertAlmostEqual(totals["debit"], totals["credit"], places=2)

        # Reconcile both subsidiary balances to their GL control accounts.
        advance_control = sum(
            (Decimal(row["credit"]) - Decimal(row["debit"]) for row in self.db.execute(
                """SELECT jl.debit,jl.credit FROM journal_lines jl
                   JOIN accounts a ON a.id=jl.account_id WHERE a.code='2160'"""
            )),
            Decimal("0"),
        )
        advance_subledger = (
            Decimal(self.db.execute(
                "SELECT amount FROM customer_advance_receipts WHERE id=?",
                (advance["advance_receipt_id"],),
            ).fetchone()["amount"])
            - sum((Decimal(row["amount"]) for row in self.db.execute(
                "SELECT amount FROM customer_advance_allocations WHERE advance_receipt_id=?",
                (advance["advance_receipt_id"],),
            )), Decimal("0"))
            - sum((Decimal(row["amount"]) for row in self.db.execute(
                "SELECT amount FROM customer_advance_refunds WHERE advance_receipt_id=?",
                (advance["advance_receipt_id"],),
            )), Decimal("0"))
        )
        self.assertEqual(advance_control, Decimal("5.00"))
        self.assertEqual(advance_subledger, advance_control)

        ar_control = sum(
            (Decimal(row["debit"]) - Decimal(row["credit"]) for row in self.db.execute(
                """SELECT jl.debit,jl.credit FROM journal_lines jl
                   JOIN accounts a ON a.id=jl.account_id WHERE a.code='1130'"""
            )),
            Decimal("0"),
        )
        ar_subledger = sum(
            (Decimal(row["total_amount"]) - Decimal(row["paid_amount"] or "0")
             - Decimal(row["credited_amount"] or "0") for row in self.db.execute(
                "SELECT total_amount,paid_amount,credited_amount FROM sales_invoices WHERE customer_id=? AND status='posted'",
                (self.customer_id,),
            )),
            Decimal("0"),
        )
        self.assertEqual(ar_control, Decimal("70.00"))
        self.assertEqual(ar_subledger, ar_control)


    def test_customer_advance_rejects_overallocation_and_cross_customer_invoice(self):
        self.db.execute("BEGIN IMMEDIATE")
        first_invoice = sales.post_invoice(
            self.db, self.customer_id, "2026-11-10", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "1", "unit_price": "100"}], "0", 1,
        )
        self.db.execute("INSERT INTO customers(code,name) VALUES('C2','عميل آخر')")
        second_customer_id = self.db.execute(
            "SELECT id FROM customers WHERE code='C2'"
        ).fetchone()["id"]
        second_invoice = sales.post_invoice(
            self.db, second_customer_id, "2026-11-10", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "1", "unit_price": "20"}], "0", 1,
        )
        cash_id = self.db.execute("SELECT id FROM accounts WHERE code='1110'").fetchone()["id"]
        advance = sales.post_customer_advance_receipt(
            self.db, self.customer_id, "2026-11-11", "50", cash_id, 1,
        )

        with self.assertRaisesRegex(ValueError, "تتجاوز المتبقي من مقدم العميل"):
            sales.allocate_customer_advance(
                self.db, advance["advance_receipt_id"], [(first_invoice["invoice_id"], "50.01")],
                "2026-11-12", 1,
            )
        with self.assertRaisesRegex(ValueError, "لا تتبع نفس العميل"):
            sales.allocate_customer_advance(
                self.db, advance["advance_receipt_id"], [(second_invoice["invoice_id"], "10")],
                "2026-11-12", 1,
            )
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM customer_advance_allocations WHERE advance_receipt_id=?",
                (advance["advance_receipt_id"],),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.db.execute(
                "SELECT paid_amount FROM sales_invoices WHERE id=?", (first_invoice["invoice_id"],)
            ).fetchone()["paid_amount"],
            "0",
        )
        self.db.rollback()

    def test_customer_advance_refund_cannot_exceed_unallocated_balance(self):
        self.db.execute("BEGIN IMMEDIATE")
        cash_id = self.db.execute("SELECT id FROM accounts WHERE code='1110'").fetchone()["id"]
        inventory_id = self.db.execute("SELECT id FROM accounts WHERE code='1150'").fetchone()["id"]
        with self.assertRaisesRegex(ValueError, "مربوطًا بملف النشاط"):
            sales.post_customer_advance_receipt(
                self.db, self.customer_id, "2026-11-11", "30", inventory_id, 1,
            )
        advance = sales.post_customer_advance_receipt(
            self.db, self.customer_id, "2026-11-11", "30", cash_id, 1,
        )
        with self.assertRaisesRegex(ValueError, "تتجاوز المتبقي من مقدم العميل"):
            sales.post_customer_advance_refund(
                self.db, advance["advance_receipt_id"], "2026-11-12", "30.01", cash_id, 1,
            )
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM customer_advance_refunds WHERE advance_receipt_id=?",
                (advance["advance_receipt_id"],),
            ).fetchone()[0],
            0,
        )
        self.db.rollback()


    def test_batch_receipt_and_advance_allocation_preserve_each_invoice_dimensions(self):
        branch_a = self.db.execute(
            "INSERT INTO branches(code,name_ar) VALUES('BR-MIX-A','فرع أ')"
        ).lastrowid
        branch_b = self.db.execute(
            "INSERT INTO branches(code,name_ar) VALUES('BR-MIX-B','فرع ب')"
        ).lastrowid
        project_a = self.db.execute(
            "INSERT INTO projects(code,name_ar) VALUES('PJ-MIX-A','مشروع أ')"
        ).lastrowid
        project_b = self.db.execute(
            "INSERT INTO projects(code,name_ar) VALUES('PJ-MIX-B','مشروع ب')"
        ).lastrowid
        self.db.execute("BEGIN IMMEDIATE")
        invoice_a = sales.post_invoice(
            self.db, self.customer_id, "2026-11-10", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "1", "unit_price": "100"}],
            "0", 1, dimensions={"branch_id": branch_a, "project_id": project_a},
        )
        invoice_b = sales.post_invoice(
            self.db, self.customer_id, "2026-11-10", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "1", "unit_price": "100"}],
            "0", 1, dimensions={"branch_id": branch_b, "project_id": project_b},
        )
        cash_id = self.db.execute(
            "SELECT id FROM accounts WHERE code='1110'"
        ).fetchone()["id"]
        advance = sales.post_customer_advance_receipt(
            self.db, self.customer_id, "2026-11-11", "100", cash_id, 1,
        )
        allocation = sales.allocate_customer_advance(
            self.db, advance["advance_receipt_id"],
            [(invoice_a["invoice_id"], "50"), (invoice_b["invoice_id"], "50")],
            "2026-11-12", 1,
        )
        batch = sales.post_customer_receipt_batch(
            self.db, self.customer_id, "2026-11-13",
            [(invoice_a["invoice_id"], "50"), (invoice_b["invoice_id"], "50")],
            cash_id, 1, "MIXED-DIMENSIONS",
        )
        self.db.commit()

        expected = {
            invoice_a["invoice_id"]: (branch_a, project_a),
            invoice_b["invoice_id"]: (branch_b, project_b),
        }
        for entry_id in (allocation["journal_entry_id"], batch["journal_entry_id"]):
            rows = self.db.execute(
                """SELECT jl.credit,jl.branch_id,jl.project_id
                   FROM journal_lines jl JOIN accounts a ON a.id=jl.account_id
                   WHERE jl.entry_id=? AND a.code='1130' ORDER BY jl.credit""",
                (entry_id,),
            ).fetchall()
            self.assertEqual(len(rows), 2)
            actual = {(row["branch_id"], row["project_id"]) for row in rows}
            self.assertEqual(actual, set(expected.values()))
            self.assertEqual(
                sum((Decimal(row["credit"]) for row in rows), Decimal("0")),
                Decimal("100.00"),
            )
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM journal_entries WHERE reference IN (?,?)",
                (allocation["journal_entry_id"] and self.db.execute(
                    "SELECT reference FROM journal_entries WHERE id=?",
                    (allocation["journal_entry_id"],),
                ).fetchone()["reference"], batch["receipt_no"]),
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            sales._customer_advance_available(self.db, advance["advance_receipt_id"]),
            Decimal("0.00"),
        )

    def test_customer_cash_references_are_unique_and_refunds_follow_credit_note_date(self):
        self.db.execute("BEGIN IMMEDIATE")
        invoice = sales.post_invoice(
            self.db, self.customer_id, "2026-11-10", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "1", "unit_price": "100"}],
            "0", 1,
        )
        cash_id = self.db.execute(
            "SELECT id FROM accounts WHERE code='1110'"
        ).fetchone()["id"]
        sales.post_customer_receipt(
            self.db, invoice["invoice_id"], "2026-11-11", "20",
            cash_id, 1, "BANK-UNIQUE-001",
        )
        sales.post_customer_receipt(
            self.db, invoice["invoice_id"], "2026-11-12", "80",
            cash_id, 1, "BANK-UNIQUE-002",
        )
        with self.assertRaisesRegex(ValueError, "مستخدم بالفعل"):
            sales.post_customer_advance_receipt(
                self.db, self.customer_id, "2026-11-12", "30",
                cash_id, 1, "BANK-UNIQUE-001",
            )
        line_id = self.db.execute(
            "SELECT id FROM sales_invoice_lines WHERE invoice_id=?",
            (invoice["invoice_id"],),
        ).fetchone()["id"]
        credit = sales.post_credit_note(
            self.db, invoice["invoice_id"], "2026-11-15", "اختبار تاريخ الرد",
            [{"invoice_line_id": line_id, "quantity": "0.5"}], 1,
        )
        with self.assertRaisesRegex(ValueError, "يسبق تاريخ الإشعار الدائن"):
            sales.post_customer_refund(
                self.db, credit["credit_note_id"], "2026-11-14", "50",
                cash_id, 1, "BANK-REFUND-EARLY",
            )
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM customer_refunds WHERE credit_note_id=?",
                (credit["credit_note_id"],),
            ).fetchone()[0],
            0,
        )
        self.db.rollback()

    def test_customer_settlement_cycle_preserves_invoice_dimensions_and_reconciles_gl(self):
        branch_id = self.db.execute(
            "INSERT INTO branches(code,name_ar) VALUES('BR-COL','فرع التحصيل')"
        ).lastrowid
        center_id = self.db.execute(
            "INSERT INTO cost_centers(code,name_ar) VALUES('CC-COL','مركز التحصيل')"
        ).lastrowid
        project_id = self.db.execute(
            "INSERT INTO projects(code,name_ar) VALUES('PJ-COL','مشروع التحصيل')"
        ).lastrowid
        salesperson_id = self.db.execute(
            "INSERT INTO salespersons(code,name) VALUES('SP-COL','مندوب التحصيل')"
        ).lastrowid
        dimensions = {
            "branch_id": branch_id, "cost_center_id": center_id,
            "project_id": project_id, "salesperson_id": salesperson_id,
        }
        self.db.execute("BEGIN IMMEDIATE")
        invoice = sales.post_invoice(
            self.db, self.customer_id, "2026-11-10", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "1", "unit_price": "100"}],
            "0", 1, dimensions=dimensions,
        )
        cash_id = self.db.execute("SELECT id FROM accounts WHERE code='1110'").fetchone()["id"]
        advance = sales.post_customer_advance_receipt(
            self.db, self.customer_id, "2026-11-11", "20", cash_id, 1,
        )
        allocation = sales.allocate_customer_advance(
            self.db, advance["advance_receipt_id"],
            [(invoice["invoice_id"], "20")], "2026-11-12", 1,
        )
        batch = sales.post_customer_receipt_batch(
            self.db, self.customer_id, "2026-11-13",
            [(invoice["invoice_id"], "80")], cash_id, 1, "DIM-BATCH",
        )
        line_id = self.db.execute(
            "SELECT id FROM sales_invoice_lines WHERE invoice_id=?",
            (invoice["invoice_id"],),
        ).fetchone()["id"]
        credit = sales.post_credit_note(
            self.db, invoice["invoice_id"], "2026-11-14", "مرتجع بعد التحصيل",
            [{"invoice_line_id": line_id, "quantity": "0.5"}], 1,
        )
        refund = sales.post_customer_refund(
            self.db, credit["credit_note_id"], "2026-11-15", "50", cash_id, 1,
        )
        self.db.commit()

        expected_dimensions = (
            branch_id, center_id, project_id, self.customer_id, salesperson_id,
        )
        for entry_id in (allocation["journal_entry_id"], batch["journal_entry_id"], refund["journal_entry_id"]):
            ar_line = self.db.execute(
                """SELECT jl.branch_id,jl.cost_center_id,jl.project_id,jl.customer_id,jl.salesperson_id
                   FROM journal_lines jl JOIN accounts a ON a.id=jl.account_id
                   WHERE jl.entry_id=? AND a.code='1130' LIMIT 1""",
                (entry_id,),
            ).fetchone()
            self.assertEqual(tuple(ar_line), expected_dimensions)

        ar_control = sum(
            (Decimal(row["debit"]) - Decimal(row["credit"]) for row in self.db.execute(
                """SELECT jl.debit,jl.credit FROM journal_lines jl
                   JOIN accounts a ON a.id=jl.account_id WHERE a.code='1130'"""
            )),
            Decimal("0"),
        )
        invoice_row = self.db.execute(
            "SELECT total_amount,paid_amount,credited_amount FROM sales_invoices WHERE id=?",
            (invoice["invoice_id"],),
        ).fetchone()
        refund_total = sum(
            (Decimal(row["amount"]) for row in self.db.execute(
                """SELECT rf.amount FROM customer_refunds rf
                   JOIN sales_credit_notes cn ON cn.id=rf.credit_note_id WHERE cn.invoice_id=?""",
                (invoice["invoice_id"],),
            )),
            Decimal("0"),
        )
        ar_subledger = (
            Decimal(invoice_row["total_amount"]) - Decimal(invoice_row["paid_amount"])
            - Decimal(invoice_row["credited_amount"]) + refund_total
        )
        self.assertEqual(ar_control, Decimal("0.00"))
        self.assertEqual(ar_subledger, ar_control)

        advance_control = sum(
            (Decimal(row["credit"]) - Decimal(row["debit"]) for row in self.db.execute(
                """SELECT jl.debit,jl.credit FROM journal_lines jl
                   JOIN accounts a ON a.id=jl.account_id WHERE a.code='2160'"""
            )),
            Decimal("0"),
        )
        self.assertEqual(advance_control, Decimal("0.00"))
        self.assertEqual(sales._customer_advance_available(self.db, advance["advance_receipt_id"]), Decimal("0.00"))

    def test_customer_advance_receipt_allocation_and_refund_can_be_reversed_safely(self):
        self.db.execute("BEGIN IMMEDIATE")
        invoice = sales.post_invoice(
            self.db, self.customer_id, "2026-11-10", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "1", "unit_price": "100"}],
            "0", 1,
        )
        cash_id = self.db.execute(
            "SELECT id FROM accounts WHERE code='1110'"
        ).fetchone()["id"]
        advance = sales.post_customer_advance_receipt(
            self.db, self.customer_id, "2026-11-11", "100", cash_id, 1,
            "ADV-REV-RECEIPT",
        )
        allocation = sales.allocate_customer_advance(
            self.db, advance["advance_receipt_id"],
            [(invoice["invoice_id"], "60")], "2026-11-12", 1,
        )
        invoice_paid = self.db.execute(
            "SELECT paid_amount FROM sales_invoices WHERE id=?",
            (invoice["invoice_id"],),
        ).fetchone()["paid_amount"]
        self.assertEqual(Decimal(invoice_paid), Decimal("60.00"))
        sales.reverse_customer_advance_transaction(
            self.db, "allocation", allocation["journal_entry_id"],
            "2026-11-13", "تصحيح تخصيص على فاتورة غير صحيحة", 1,
        )
        self.assertEqual(
            Decimal(self.db.execute(
                "SELECT paid_amount FROM sales_invoices WHERE id=?",
                (invoice["invoice_id"],),
            ).fetchone()["paid_amount"]),
            Decimal("0.00"),
        )
        self.assertEqual(
            sales._customer_advance_available(self.db, advance["advance_receipt_id"]),
            Decimal("100.00"),
        )
        self.assertEqual(
            party_reports._customer_advance_subledger(self.db, "2026-11-12"),
            (Decimal("40.00"), True),
        )
        self.assertEqual(
            party_reports._customer_advance_subledger(self.db, "2026-11-13"),
            (Decimal("100.00"), True),
        )
        with self.assertRaisesRegex(ValueError, "من قبل"):
            sales.reverse_customer_advance_transaction(
                self.db, "allocation", allocation["journal_entry_id"],
                "2026-11-14", "محاولة عكس مكرر", 1,
            )

        refund = sales.post_customer_advance_refund(
            self.db, advance["advance_receipt_id"], "2026-11-14",
            "20", cash_id, 1, "ADV-REF-TO-REVERSE",
        )
        sales.reverse_customer_advance_transaction(
            self.db, "refund",
            self.db.execute(
                "SELECT id FROM customer_advance_refunds WHERE refund_no=?",
                (refund["refund_no"],),
            ).fetchone()["id"],
            "2026-11-15", "إلغاء رد المقدم بالخطأ", 1,
        )
        self.assertEqual(
            sales._customer_advance_available(self.db, advance["advance_receipt_id"]),
            Decimal("100.00"),
        )
        receipt_reversal = sales.reverse_customer_advance_transaction(
            self.db, "receipt", advance["advance_receipt_id"],
            "2026-11-16", "إلغاء إيصال المقدم الأصلي", 1,
        )
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM journal_entries WHERE id IN (?,?)",
                (advance["journal_entry_id"], receipt_reversal["reversal_journal_entry_id"]),
            ).fetchone()[0],
            2,
        )
        with self.assertRaisesRegex(ValueError, "تم عكسه"):
            sales._customer_advance_available(self.db, advance["advance_receipt_id"])
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM customer_advance_reversals"
            ).fetchone()[0],
            3,
        )
        self.db.rollback()

    def test_customer_advance_dates_cannot_precede_receipt_or_invoice(self):
        self.db.execute("BEGIN IMMEDIATE")
        first_invoice = sales.post_invoice(
            self.db, self.customer_id, "2026-11-10", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "1", "unit_price": "100"}], "0", 1,
        )
        future_invoice = sales.post_invoice(
            self.db, self.customer_id, "2026-11-15", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "1", "unit_price": "50"}], "0", 1,
        )
        cash_id = self.db.execute("SELECT id FROM accounts WHERE code='1110'").fetchone()["id"]
        advance = sales.post_customer_advance_receipt(
            self.db, self.customer_id, "2026-11-11", "100", cash_id, 1,
        )
        with self.assertRaisesRegex(ValueError, "يسبق تاريخ استلامه"):
            sales.allocate_customer_advance(
                self.db, advance["advance_receipt_id"],
                [(first_invoice["invoice_id"], "10")], "2026-11-10", 1,
            )
        with self.assertRaisesRegex(ValueError, "قبل تاريخ إصدارها"):
            sales.allocate_customer_advance(
                self.db, advance["advance_receipt_id"],
                [(future_invoice["invoice_id"], "10")], "2026-11-12", 1,
            )
        with self.assertRaisesRegex(ValueError, "يسبق تاريخ استلامه"):
            sales.post_customer_advance_refund(
                self.db, advance["advance_receipt_id"], "2026-11-10", "10", cash_id, 1,
            )
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM customer_advance_allocations WHERE advance_receipt_id=?",
                (advance["advance_receipt_id"],),
            ).fetchone()[0], 0,
        )
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM customer_advance_refunds WHERE advance_receipt_id=?",
                (advance["advance_receipt_id"],),
            ).fetchone()[0], 0,
        )
        self.db.rollback()

    def test_customer_receipts_require_mapped_cash_accounts_and_valid_values(self):
        self.db.execute("BEGIN IMMEDIATE")
        invoice = sales.post_invoice(
            self.db, self.customer_id, "2026-11-10", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "1", "unit_price": "100"}], "0", 1,
        )
        inventory_id = self.db.execute("SELECT id FROM accounts WHERE code='1150'").fetchone()["id"]
        with self.assertRaisesRegex(ValueError, "مربوطًا بملف النشاط"):
            sales.post_customer_receipt(
                self.db, invoice["invoice_id"], "2026-11-11", "10", inventory_id, 1,
            )
        with self.assertRaisesRegex(ValueError, "مربوطًا بملف النشاط"):
            sales.post_customer_receipt_batch(
                self.db, self.customer_id, "2026-11-11",
                [(invoice["invoice_id"], "10")], inventory_id, 1,
            )
        cash_id = self.db.execute("SELECT id FROM accounts WHERE code='1110'").fetchone()["id"]
        with self.assertRaisesRegex(ValueError, "قيمة التحصيل"):
            sales.post_customer_receipt(
                self.db, invoice["invoice_id"], "2026-11-11", "NaN", cash_id, 1,
            )
        with self.assertRaisesRegex(ValueError, "تاريخ العملية غير صحيح"):
            sales.post_customer_receipt(
                self.db, invoice["invoice_id"], "not-a-date", "10", cash_id, 1,
            )
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM customer_receipts").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM customer_receipt_batches").fetchone()[0], 0)
        self.db.rollback()

    def test_customer_advance_routes_receive_allocate_and_refund(self):
        from flask import Flask
        app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / "templates"))
        app.secret_key = "test"
        app.add_url_rule("/login", endpoint="login", view_func=lambda: "")
        app.add_url_rule("/dashboard", endpoint="dashboard", view_func=lambda: "")
        app.add_url_rule("/global-search", endpoint="global_search", view_func=lambda: "")
        app.add_url_rule("/change-password", endpoint="change_password", view_func=lambda: "")
        app.add_url_rule("/logout", endpoint="logout", view_func=lambda: "")
        app.add_url_rule("/about", endpoint="about", view_func=lambda: "")
        app.add_url_rule("/license", endpoint="license_page", view_func=lambda: "")
        app.add_url_rule("/aging", endpoint="aging.index", view_func=lambda: "")
        app.jinja_env.globals["csrf_token"] = lambda: "test-token"
        app.context_processor(lambda: {
            "permissions": {"sales.view", "sales.manage"}, "license_info": {"status": "demo"}
        })
        app.register_blueprint(sales.sales_bp)
        client = app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = 1
            session["role"] = "accountant"

        self.db.execute("BEGIN IMMEDIATE")
        invoice = sales.post_invoice(
            self.db, self.customer_id, "2026-11-10", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "1", "unit_price": "100"}], "0", 1,
        )
        second_invoice = sales.post_invoice(
            self.db, self.customer_id, "2026-11-10", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "1", "unit_price": "50"}], "0", 1,
        )
        self.db.commit()
        cash_id = self.db.execute("SELECT id FROM accounts WHERE code='1110'").fetchone()["id"]

        response = client.post("/sales/advances/receipt", data={
            "customer_id": str(self.customer_id), "receipt_date": "2026-11-11",
            "amount": "75", "cash_account_id": str(cash_id), "reference": "WEB-ADV-1",
        })
        self.assertEqual(response.status_code, 302)
        advance = self.db.execute(
            "SELECT * FROM customer_advance_receipts WHERE reference='WEB-ADV-1'"
        ).fetchone()
        self.assertIsNotNone(advance)
        page = client.get("/sales/advances")
        self.assertEqual(page.status_code, 200)
        self.assertIn("دفعات العملاء المقدمة", page.get_data(as_text=True))

        response = client.post(
            f"/sales/advances/{advance['id']}/allocate",
            data={
                "invoice_ids": [str(invoice["invoice_id"]), str(second_invoice["invoice_id"])],
                "allocation_amount_" + str(invoice["invoice_id"]): "40",
                "allocation_amount_" + str(second_invoice["invoice_id"]): "20",
                "allocation_date": "2026-11-12",
            },
        )
        self.assertEqual(response.status_code, 302)
        paid = self.db.execute(
            "SELECT paid_amount FROM sales_invoices WHERE id=?", (invoice["invoice_id"],)
        ).fetchone()["paid_amount"]
        second_paid = self.db.execute(
            "SELECT paid_amount FROM sales_invoices WHERE id=?", (second_invoice["invoice_id"],)
        ).fetchone()["paid_amount"]
        self.assertEqual(Decimal(paid), Decimal("40.00"))
        self.assertEqual(Decimal(second_paid), Decimal("20.00"))
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM customer_advance_allocations WHERE advance_receipt_id=?",
                (advance["id"],),
            ).fetchone()[0],
            2,
        )

        response = client.post(
            f"/sales/advances/{advance['id']}/refund",
            data={"refund_date": "2026-11-13", "amount": "10",
                  "cash_account_id": str(cash_id), "reference": "WEB-REF-1"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM customer_advance_refunds WHERE reference='WEB-REF-1'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(sales._customer_advance_available(self.db, advance["id"]), Decimal("5.00"))

        allocation_journal_id = self.db.execute(
            "SELECT journal_entry_id FROM customer_advance_allocations WHERE advance_receipt_id=? LIMIT 1",
            (advance["id"],),
        ).fetchone()["journal_entry_id"]
        response = client.post(
            f"/sales/advances/reversals/allocation/{allocation_journal_id}",
            data={"reversal_date": "2026-11-14", "reason": "تصحيح تخصيص تجريبي خاطئ"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            Decimal(self.db.execute(
                "SELECT paid_amount FROM sales_invoices WHERE id=?",
                (invoice["invoice_id"],),
            ).fetchone()["paid_amount"]),
            Decimal("0.00"),
        )
        self.assertEqual(sales._customer_advance_available(self.db, advance["id"]), Decimal("65.00"))

        refund_id = self.db.execute(
            "SELECT id FROM customer_advance_refunds WHERE reference='WEB-REF-1'"
        ).fetchone()["id"]
        response = client.post(
            f"/sales/advances/reversals/refund/{refund_id}",
            data={"reversal_date": "2026-11-15", "reason": "إلغاء رد المقدم التجريبي"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(sales._customer_advance_available(self.db, advance["id"]), Decimal("75.00"))

        response = client.post(
            f"/sales/advances/reversals/receipt/{advance['id']}",
            data={"reversal_date": "2026-11-16", "reason": "إلغاء إيصال المقدم التجريبي"},
        )
        self.assertEqual(response.status_code, 302)
        with self.assertRaisesRegex(ValueError, "تم عكسه"):
            sales._customer_advance_available(self.db, advance["id"])



    def _create_accepted_quote_and_order(self, quantity="4"):
        quotations.DB_PATH = sales.DB_PATH
        quotations.initialize_quotes_schema()
        today = date.today()
        cur = self.db.execute(
            """INSERT INTO quotations(quote_no,customer_id,quote_date,valid_until,status,
               subtotal,tax_rate,tax_amount,total_amount,notes,created_by)
               VALUES('QT-TEST-1',?,?,?,'accepted',?, '14', ?, ?, '', 1)""",
            (self.customer_id, today.isoformat(), (today + timedelta(days=30)).isoformat(),
             str(Decimal(quantity) * Decimal("20")),
             str(Decimal(quantity) * Decimal("20") * Decimal("0.14")),
             str(Decimal(quantity) * Decimal("20") * Decimal("1.14"))),
        )
        quote_id = cur.lastrowid
        self.db.execute(
            """INSERT INTO quotation_lines(quotation_id,product_id,description,quantity,unit_price,line_total)
               VALUES(?,?,?,?,'20',?)""",
            (quote_id, self.product_id, "صنف اختبار", quantity,
             str(Decimal(quantity) * Decimal("20"))),
        )
        order = quotations.create_sales_order_from_quotation(
            self.db, quote_id, created_by=1, warehouse_id=self.warehouse_id,
        )
        self.db.commit()
        order_line_id = self.db.execute(
            "SELECT id FROM sales_order_lines WHERE sales_order_id=?",
            (order["sales_order_id"],),
        ).fetchone()["id"]
        return quote_id, order["sales_order_id"], order_line_id

    def test_discount_rejection_updates_request_and_sales_order_together(self):
        self.db.execute(
            "UPDATE customers SET discount_type='percent',discount_value='20' WHERE id=?",
            (self.customer_id,),
        )
        self.db.execute(
            "INSERT INTO company_settings(setting_key,setting_value) VALUES('sales_discount_limit_percent','10')"
        )
        _, order_id, _ = self._create_accepted_quote_and_order("4")
        request_id = self.db.execute(
            "SELECT discount_approval_request_id FROM sales_orders WHERE id=?", (order_id,)
        ).fetchone()["discount_approval_request_id"]
        current = self.db.execute(
            "SELECT * FROM sales_discount_approval_requests WHERE id=?", (request_id,)
        ).fetchone()

        self.db.execute("BEGIN IMMEDIATE")
        sales.reject_discount_approval_request(
            self.db, current, reviewer_id=2, reason="الخصم أعلى من السياسة المعتمدة", request_id=request_id
        )
        self.db.commit()

        request_row = self.db.execute(
            "SELECT status,reviewed_by,decision_reason FROM sales_discount_approval_requests WHERE id=?",
            (request_id,),
        ).fetchone()
        order = self.db.execute(
            "SELECT status,discount_approval_status,rejected_by,rejection_reason FROM sales_orders WHERE id=?",
            (order_id,),
        ).fetchone()
        self.assertEqual(tuple(request_row), ("rejected", 2, "الخصم أعلى من السياسة المعتمدة"))
        self.assertEqual(tuple(order), (
            "rejected", "rejected", 2, "الخصم أعلى من السياسة المعتمدة"
        ))

    def test_stale_sales_order_discount_request_cannot_be_rejected(self):
        self.db.execute(
            "UPDATE customers SET discount_type='percent',discount_value='20' WHERE id=?",
            (self.customer_id,),
        )
        self.db.execute(
            "INSERT INTO company_settings(setting_key,setting_value) VALUES('sales_discount_limit_percent','10')"
        )
        _, order_id, _ = self._create_accepted_quote_and_order("4")
        request_id = self.db.execute(
            "SELECT discount_approval_request_id FROM sales_orders WHERE id=?", (order_id,)
        ).fetchone()["discount_approval_request_id"]
        current = self.db.execute(
            "SELECT * FROM sales_discount_approval_requests WHERE id=?", (request_id,)
        ).fetchone()
        self.db.execute("UPDATE sales_orders SET status='cancelled' WHERE id=?", (order_id,))
        self.db.commit()

        with self.assertRaisesRegex(ValueError, "لم يعد صالحًا"):
            sales.reject_discount_approval_request(
                self.db, current, reviewer_id=2, reason="رفض", request_id=request_id
            )
        self.assertEqual(
            self.db.execute(
                "SELECT status FROM sales_discount_approval_requests WHERE id=?", (request_id,)
            ).fetchone()["status"],
            "pending",
        )

    def test_sales_order_discount_over_limit_requires_separate_approval_before_order_or_stock(self):
        quotations.DB_PATH = sales.DB_PATH
        quotations.initialize_quotes_schema()
        self.db.execute(
            "UPDATE customers SET discount_type='percent',discount_value='20' WHERE id=?",
            (self.customer_id,),
        )
        self.db.execute(
            "INSERT INTO company_settings(setting_key,setting_value) VALUES('sales_discount_limit_percent','10')"
        )
        quote_id, order_id, order_line_id = self._create_accepted_quote_and_order("4")
        order = self.db.execute(
            "SELECT discount_approval_status,discount_approval_request_id,status FROM sales_orders WHERE id=?",
            (order_id,),
        ).fetchone()
        self.assertEqual(order["discount_approval_status"], "pending")
        self.assertEqual(order["status"], "pending_approval")
        request_row = self.db.execute(
            "SELECT request_type,sales_order_id,status FROM sales_discount_approval_requests WHERE id=?",
            (order["discount_approval_request_id"],),
        ).fetchone()
        self.assertEqual(tuple(request_row), ("sales_order", order_id, "pending"))

        with self.assertRaisesRegex(ValueError, "قبل اعتماد الخصم"):
            quotations.approve_sales_order(self.db, order_id, approver_id=2)
        with self.assertRaisesRegex(ValueError, "قبل اعتماد الخصم"):
            quotations.create_delivery_note(
                self.db, order_id, date.today().isoformat(),
                [{"sales_order_line_id": order_line_id, "quantity": "1"}], created_by=2,
            )
        with self.assertRaisesRegex(ValueError, "قبل اعتماد الخصم"):
            quotations.invoice_sales_order(
                self.db, order_id, date.today().isoformat(), date.today().isoformat(), created_by=2,
            )
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM stock_movements WHERE movement_type='issue'").fetchone()[0],
            0,
        )
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0], 0)

        self.db.execute(
            "UPDATE sales_discount_approval_requests SET status='approved',reviewed_by=2 WHERE id=?",
            (order["discount_approval_request_id"],),
        )
        self.db.execute(
            "UPDATE sales_orders SET discount_approval_status='approved' WHERE id=?",
            (order_id,),
        )
        quotations.approve_sales_order(self.db, order_id, approver_id=2)
        self.assertEqual(
            self.db.execute("SELECT status FROM sales_orders WHERE id=?", (order_id,)).fetchone()["status"],
            "approved",
        )

    def test_sales_order_partial_delivery_and_invoicing_never_issue_stock_twice(self):
        _, order_id, order_line_id = self._create_accepted_quote_and_order("4")
        base_date = date.today()
        first_delivery_date = (base_date + timedelta(days=1)).isoformat()
        first_invoice_date = (base_date + timedelta(days=2)).isoformat()
        credit_date = (base_date + timedelta(days=3)).isoformat()
        second_delivery_date = (base_date + timedelta(days=4)).isoformat()
        second_invoice_date = (base_date + timedelta(days=5)).isoformat()
        with self.assertRaisesRegex(ValueError, "نفس منشئه"):
            quotations.approve_sales_order(self.db, order_id, approver_id=1)
        self.assertEqual(
            self.db.execute("SELECT status FROM sales_orders WHERE id=?", (order_id,)).fetchone()["status"],
            "pending_approval",
        )
        quotations.approve_sales_order(self.db, order_id, approver_id=2)
        self.db.commit()

        self.db.execute("BEGIN IMMEDIATE")
        first_note = quotations.create_delivery_note(
            self.db, order_id, first_delivery_date,
            [{"sales_order_line_id": order_line_id, "quantity": "2"}], 2,
        )
        # A draft delivery note must not move stock or create a journal.
        self.assertEqual(
            Decimal(self.db.execute("SELECT qty_remaining FROM stock_valuation_layers").fetchone()[0]),
            Decimal("5"),
        )
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM stock_movements WHERE movement_type='issue'"
        ).fetchone()[0], 0)
        quotations.post_delivery_note(self.db, first_note["delivery_note_id"], posted_by=2)
        self.db.commit()

        self.assertEqual(
            Decimal(self.db.execute("SELECT qty_remaining FROM stock_valuation_layers").fetchone()[0]),
            Decimal("3.0000"),
        )
        with self.assertRaisesRegex(ValueError, "يسبق تاريخ إذن التسليم"):
            quotations.invoice_sales_order(
                self.db, order_id, base_date.isoformat(), (base_date + timedelta(days=30)).isoformat(), created_by=2,
            )
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM sales_invoices WHERE sales_order_id=?", (order_id,)
        ).fetchone()[0], 0)
        first_invoice = quotations.invoice_sales_order(
            self.db, order_id, first_invoice_date, (base_date + timedelta(days=30)).isoformat(), created_by=2,
        )
        self.db.commit()
        self.assertEqual(first_invoice["total_cost"], Decimal("20.00"))
        self.assertIsNone(first_invoice["cogs_journal_entry_id"])
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM stock_movements WHERE movement_type='issue'"
        ).fetchone()[0], 1)
        invoice_line_id = self.db.execute(
            "SELECT id FROM sales_invoice_lines WHERE invoice_id=?", (first_invoice["invoice_id"],)
        ).fetchone()["id"]
        self.db.execute("BEGIN IMMEDIATE")
        credit = sales.post_credit_note(
            self.db, first_invoice["invoice_id"], credit_date, "مرتجع من فاتورة أمر البيع",
            [{"invoice_line_id": invoice_line_id, "quantity": "1"}], 2,
        )
        self.db.commit()
        self.assertEqual(credit["total_cost"], Decimal("10.00"))
        self.assertEqual(
            Decimal(self.db.execute("SELECT SUM(CAST(qty_remaining AS REAL)) FROM stock_valuation_layers").fetchone()[0]),
            Decimal("4"),
        )
        with self.assertRaisesRegex(ValueError, "تم ترحيله"):
            quotations.post_delivery_note(self.db, first_note["delivery_note_id"], posted_by=2)

        self.db.execute("BEGIN IMMEDIATE")
        second_note = quotations.create_delivery_note(
            self.db, order_id, second_delivery_date,
            [{"sales_order_line_id": order_line_id, "quantity": "2"}], 2,
        )
        quotations.post_delivery_note(self.db, second_note["delivery_note_id"], posted_by=2)
        second_invoice = quotations.invoice_sales_order(
            self.db, order_id, second_invoice_date, (base_date + timedelta(days=30)).isoformat(), created_by=2,
        )
        self.db.commit()

        self.assertEqual(second_invoice["total_cost"], Decimal("20.00"))
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM stock_movements WHERE movement_type='issue'"
        ).fetchone()[0], 2)
        self.assertEqual(
            Decimal(self.db.execute(
                "SELECT SUM(CAST(qty_remaining AS NUMERIC)) FROM stock_valuation_layers"
            ).fetchone()[0]),
            Decimal("2.0000"),
        )
        self.assertEqual(
            self.db.execute("SELECT status FROM sales_orders WHERE id=?", (order_id,)).fetchone()["status"],
            "invoiced",
        )
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM sales_invoices WHERE sales_order_id=?", (order_id,)
        ).fetchone()[0], 2)
        invoice_cost = sum(
            (Decimal(row["cost_total"]) for row in self.db.execute(
                "SELECT cost_total FROM sales_invoice_lines WHERE sales_order_line_id=?",
                (order_line_id,),
            ).fetchall()),
            Decimal("0"),
        )
        self.assertEqual(invoice_cost, Decimal("40.00"))
        cogs_debit = sum(
            (Decimal(row["debit"] or "0") for row in self.db.execute(
                """SELECT jl.debit FROM journal_lines jl JOIN accounts a ON a.id=jl.account_id
                   WHERE a.code='5100'"""
            ).fetchall()),
            Decimal("0"),
        )
        self.assertEqual(cogs_debit, Decimal("40.00"))
        cogs_credit = sum(
            (Decimal(row["credit"] or "0") for row in self.db.execute(
                """SELECT jl.credit FROM journal_lines jl JOIN accounts a ON a.id=jl.account_id
                   WHERE a.code='5100'"""
            ).fetchall()),
            Decimal("0"),
        )
        self.assertEqual(cogs_credit, Decimal("10.00"))
        self.assertEqual(
            Decimal(self.db.execute("SELECT SUM(CAST(qty_remaining AS REAL)) FROM stock_valuation_layers").fetchone()[0]),
            Decimal("2"),
        )

    def test_sales_delivery_rejects_overdelivery_and_rolls_back_insufficient_stock(self):
        _, order_id, order_line_id = self._create_accepted_quote_and_order("6")
        delivery_date = (date.today() + timedelta(days=1)).isoformat()
        second_delivery_date = (date.today() + timedelta(days=2)).isoformat()
        quotations.approve_sales_order(self.db, order_id, approver_id=2)
        self.db.commit()

        self.db.execute("BEGIN IMMEDIATE")
        with self.assertRaisesRegex(ValueError, "تتجاوز الكمية المتبقية"):
            quotations.create_delivery_note(
                self.db, order_id, delivery_date,
                [{"sales_order_line_id": order_line_id, "quantity": "7"}], 2,
            )
        self.db.rollback()
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM delivery_notes WHERE sales_order_id=?", (order_id,)
        ).fetchone()[0], 0)

        self.db.execute("BEGIN IMMEDIATE")
        reserved_note = quotations.create_delivery_note(
            self.db, order_id, delivery_date,
            [{"sales_order_line_id": order_line_id, "quantity": "4"}], 2,
        )
        self.db.commit()
        self.db.execute("BEGIN IMMEDIATE")
        with self.assertRaisesRegex(ValueError, "تتجاوز الكمية المتبقية"):
            quotations.create_delivery_note(
                self.db, order_id, delivery_date,
                [{"sales_order_line_id": order_line_id, "quantity": "3"}], 2,
            )
        self.db.rollback()
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM delivery_notes WHERE sales_order_id=?", (order_id,)
        ).fetchone()[0], 1)

        self.db.execute("BEGIN IMMEDIATE")
        quotations.post_delivery_note(self.db, reserved_note["delivery_note_id"], posted_by=2)
        self.db.commit()
        self.assertEqual(
            Decimal(self.db.execute("SELECT qty_remaining FROM stock_valuation_layers").fetchone()[0]),
            Decimal("1.0000"),
        )

        self.db.execute("BEGIN IMMEDIATE")
        note = quotations.create_delivery_note(
            self.db, order_id, second_delivery_date,
            [{"sales_order_line_id": order_line_id, "quantity": "2"}], 2,
        )
        with self.assertRaisesRegex(ValueError, "الرصيد غير كافٍ"):
            quotations.post_delivery_note(self.db, note["delivery_note_id"], posted_by=2)
        self.db.rollback()
        self.assertEqual(
            Decimal(self.db.execute("SELECT qty_remaining FROM stock_valuation_layers").fetchone()[0]),
            Decimal("1.0000"),
        )
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM stock_movements WHERE movement_type='issue'"
        ).fetchone()[0], 1)


    def test_fixed_customer_discount_is_locked_and_allocated_once_across_partial_invoices(self):
        # This test isolates discount allocation mechanics from the approval threshold.
        self.db.execute(
            "INSERT INTO company_settings(setting_key,setting_value) VALUES('sales_discount_limit_percent','100')"
        )
        self.db.execute(
            "UPDATE customers SET discount_type='fixed',discount_value='10' WHERE id=?",
            (self.customer_id,),
        )
        _, order_id, order_line_id = self._create_accepted_quote_and_order("4")
        order_discount = self.db.execute(
            "SELECT discount_amount FROM sales_orders WHERE id=?", (order_id,)
        ).fetchone()["discount_amount"]
        self.assertEqual(Decimal(order_discount), Decimal("10.00"))
        # A later master-data change must not rewrite an already accepted order.
        self.db.execute(
            "UPDATE customers SET discount_value='20' WHERE id=?", (self.customer_id,)
        )
        quotations.approve_sales_order(self.db, order_id, approver_id=2)
        self.db.commit()
        base = date.today()

        self.db.execute("BEGIN IMMEDIATE")
        first_note = quotations.create_delivery_note(
            self.db, order_id, (base + timedelta(days=1)).isoformat(),
            [{"sales_order_line_id": order_line_id, "quantity": "2"}], 2,
        )
        quotations.post_delivery_note(self.db, first_note["delivery_note_id"], posted_by=2)
        first_invoice = quotations.invoice_sales_order(
            self.db, order_id, (base + timedelta(days=2)).isoformat(),
            (base + timedelta(days=30)).isoformat(), created_by=2,
        )
        self.db.commit()
        self.assertEqual(first_invoice["discount_amount"], Decimal("5.00"))
        self.assertEqual(first_invoice["subtotal"], Decimal("35.00"))

        self.db.execute("BEGIN IMMEDIATE")
        second_note = quotations.create_delivery_note(
            self.db, order_id, (base + timedelta(days=3)).isoformat(),
            [{"sales_order_line_id": order_line_id, "quantity": "2"}], 2,
        )
        quotations.post_delivery_note(self.db, second_note["delivery_note_id"], posted_by=2)
        second_invoice = quotations.invoice_sales_order(
            self.db, order_id, (base + timedelta(days=4)).isoformat(),
            (base + timedelta(days=30)).isoformat(), created_by=2,
        )
        self.db.commit()
        self.assertEqual(second_invoice["discount_amount"], Decimal("5.00"))
        self.assertEqual(second_invoice["subtotal"], Decimal("35.00"))
        self.assertEqual(
            Decimal(self.db.execute(
                "SELECT discount_allocated FROM sales_orders WHERE id=?", (order_id,)
            ).fetchone()["discount_allocated"]),
            Decimal("10.00"),
        )
        self.assertEqual(
            sum((Decimal(row["discount_amount"]) for row in self.db.execute(
                "SELECT discount_amount FROM sales_invoices WHERE sales_order_id=?", (order_id,)
            ).fetchall()), Decimal("0")),
            Decimal("10.00"),
        )



    def test_sales_order_rejection_requires_reason_and_preserves_no_stock_or_gl_effect(self):
        _, order_id, _ = self._create_accepted_quote_and_order("2")
        with self.assertRaisesRegex(ValueError, "نفس منشئه"):
            quotations.reject_sales_order(self.db, order_id, rejected_by=1, reason="رفض")
        with self.assertRaisesRegex(ValueError, "سبب رفض أمر البيع مطلوب"):
            quotations.reject_sales_order(self.db, order_id, rejected_by=2, reason="لا")
        quotations.reject_sales_order(
            self.db, order_id, rejected_by=2, reason="مراجعة السعر والموافقة التجارية غير مكتملة",
        )
        self.db.commit()
        order = self.db.execute(
            "SELECT status,rejection_reason,rejected_by FROM sales_orders WHERE id=?", (order_id,)
        ).fetchone()
        self.assertEqual(order["status"], "rejected")
        self.assertEqual(order["rejected_by"], 2)
        self.assertEqual(order["rejection_reason"], "مراجعة السعر والموافقة التجارية غير مكتملة")
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM stock_movements WHERE movement_type='issue'"
        ).fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0], 0)
        with self.assertRaisesRegex(ValueError, "ليس في انتظار الاعتماد"):
            quotations.approve_sales_order(self.db, order_id, approver_id=2)


    def test_sales_order_to_collection_posts_complete_gl_and_trial_balance(self):
        _, order_id, order_line_id = self._create_accepted_quote_and_order("2")
        base = date.today()
        delivery_date = (base + timedelta(days=1)).isoformat()
        invoice_date = (base + timedelta(days=2)).isoformat()
        receipt_date = (base + timedelta(days=3)).isoformat()

        # Approval is operational only: no stock movement or journal is created.
        quotations.approve_sales_order(self.db, order_id, approver_id=2)
        self.db.commit()
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0], 0
        )
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM stock_movements WHERE movement_type='issue'"
            ).fetchone()[0],
            0,
        )

        self.db.execute("BEGIN IMMEDIATE")
        note = quotations.create_delivery_note(
            self.db, order_id, delivery_date,
            [{"sales_order_line_id": order_line_id, "quantity": "2"}], 2,
        )
        quotations.post_delivery_note(self.db, note["delivery_note_id"], posted_by=2)
        self.db.commit()

        delivery = self.db.execute(
            "SELECT status FROM delivery_notes WHERE id=?", (note["delivery_note_id"],)
        ).fetchone()
        self.assertEqual(delivery["status"], "posted")
        issue_rows = self.db.execute(
            """SELECT movement_no,total_cost,accounting_status,journal_entry_id
               FROM stock_movements WHERE movement_type='issue'"""
        ).fetchall()
        self.assertEqual(len(issue_rows), 1)
        self.assertEqual(Decimal(issue_rows[0]["total_cost"]), Decimal("20.00"))
        self.assertEqual(issue_rows[0]["accounting_status"], "posted")
        self.assertIsNotNone(issue_rows[0]["journal_entry_id"])

        # Invoicing recognizes revenue, receivable and VAT, but must not issue
        # inventory or recognize COGS for a second time.
        self.db.execute("BEGIN IMMEDIATE")
        invoice = quotations.invoice_sales_order(
            self.db, order_id, invoice_date,
            (base + timedelta(days=30)).isoformat(), created_by=2,
        )
        self.db.commit()
        self.assertEqual(invoice["subtotal"], Decimal("40.00"))
        self.assertEqual(invoice["tax_amount"], Decimal("5.60"))
        self.assertEqual(invoice["total_amount"], Decimal("45.60"))
        self.assertEqual(invoice["total_cost"], Decimal("20.00"))
        self.assertIsNone(invoice["cogs_journal_entry_id"])
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM stock_movements WHERE movement_type='issue'"
            ).fetchone()[0],
            1,
        )

        invoice_row = self.db.execute(
            """SELECT status,paid_amount,journal_entry_id,sales_order_id
               FROM sales_invoices WHERE id=?""",
            (invoice["invoice_id"],),
        ).fetchone()
        self.assertEqual(invoice_row["status"], "posted")
        self.assertEqual(invoice_row["sales_order_id"], order_id)
        self.assertEqual(Decimal(invoice_row["paid_amount"]), Decimal("0"))
        invoice_line = self.db.execute(
            """SELECT sales_order_line_id,quantity,line_total,cost_total
               FROM sales_invoice_lines WHERE invoice_id=?""",
            (invoice["invoice_id"],),
        ).fetchone()
        self.assertEqual(invoice_line["sales_order_line_id"], order_line_id)
        self.assertEqual(Decimal(invoice_line["quantity"]), Decimal("2"))
        self.assertEqual(Decimal(invoice_line["line_total"]), Decimal("40.00"))
        self.assertEqual(Decimal(invoice_line["cost_total"]), Decimal("20.00"))

        cash_id = self.db.execute(
            "SELECT id FROM accounts WHERE code='1110'"
        ).fetchone()["id"]
        self.db.execute("BEGIN IMMEDIATE")
        receipt = sales.post_customer_receipt_batch(
            self.db, self.customer_id, receipt_date,
            [(invoice["invoice_id"], "45.60")], cash_id, 2, "SO-E2E-COLLECTION",
        )
        self.db.commit()
        self.assertEqual(receipt["total_amount"], Decimal("45.60"))
        self.assertEqual(receipt["invoice_count"], 1)
        paid = self.db.execute(
            "SELECT paid_amount FROM sales_invoices WHERE id=?",
            (invoice["invoice_id"],),
        ).fetchone()["paid_amount"]
        self.assertEqual(Decimal(paid), Decimal("45.60"))
        self.assertEqual(
            self.db.execute(
                """SELECT COUNT(*) FROM customer_receipt_allocations a
                   JOIN customer_receipt_batches b ON b.id=a.batch_id
                   WHERE b.receipt_no=? AND a.invoice_id=?""",
                (receipt["receipt_no"], invoice["invoice_id"]),
            ).fetchone()[0],
            1,
        )

        # Verify the accounting trail from source documents through GL and TB.
        order_status = self.db.execute(
            "SELECT status FROM sales_orders WHERE id=?", (order_id,)
        ).fetchone()["status"]
        self.assertEqual(order_status, "invoiced")
        account_totals = {}
        for row in self.db.execute(
            """SELECT a.code,jl.debit,jl.credit
               FROM journal_lines jl
               JOIN journal_entries je ON je.id=jl.entry_id
               JOIN accounts a ON a.id=jl.account_id
               WHERE je.status='posted'"""
        ):
            debit, credit = account_totals.get(
                row["code"], (Decimal("0"), Decimal("0"))
            )
            account_totals[row["code"]] = (
                debit + Decimal(str(row["debit"] or "0")),
                credit + Decimal(str(row["credit"] or "0")),
            )

        self.assertEqual(account_totals["1130"], (Decimal("45.60"), Decimal("45.60")))
        self.assertEqual(account_totals["1110"], (Decimal("45.60"), Decimal("0")))
        self.assertEqual(account_totals["4100"], (Decimal("0"), Decimal("40.00")))
        self.assertEqual(account_totals["2130"], (Decimal("0"), Decimal("5.60")))
        self.assertEqual(account_totals["5100"], (Decimal("20.00"), Decimal("0")))
        self.assertEqual(account_totals["1150"], (Decimal("0"), Decimal("20.00")))

        tb_debit = sum((values[0] for values in account_totals.values()), Decimal("0"))
        tb_credit = sum((values[1] for values in account_totals.values()), Decimal("0"))
        self.assertEqual(tb_debit, Decimal("111.20"))
        self.assertEqual(tb_credit, Decimal("111.20"))
        self.assertEqual(tb_debit, tb_credit)



    def test_mixed_service_order_preserves_partial_invoice_state_after_later_delivery(self):
        _, order_id, stock_line_id = self._create_accepted_quote_and_order("4")
        base = date.today()
        service_quote_line = self.db.execute(
            """INSERT INTO quotation_lines(quotation_id,product_id,description,quantity,unit_price,line_total)
               SELECT quotation_id,NULL,'خدمة نقل', '1','20','20'
               FROM sales_orders WHERE id=?""",
            (order_id,),
        ).lastrowid
        self.db.execute(
            """INSERT INTO sales_order_lines(sales_order_id,quotation_line_id,product_id,description,
               ordered_qty,ordered_value,unit_price) VALUES(?,?,NULL,'خدمة نقل','1','20','20')""",
            (order_id, service_quote_line),
        )
        self.db.commit()

        quotations.approve_sales_order(self.db, order_id, approver_id=2)
        self.db.commit()
        first_date = (base + timedelta(days=1)).isoformat()
        self.db.execute("BEGIN IMMEDIATE")
        first = quotations.create_delivery_note(
            self.db, order_id, first_date,
            [{"sales_order_line_id": stock_line_id, "quantity": "2"}], 2,
        )
        quotations.post_delivery_note(self.db, first["delivery_note_id"], posted_by=2)
        self.db.commit()
        self.assertEqual(
            self.db.execute("SELECT status FROM sales_orders WHERE id=?", (order_id,)).fetchone()["status"],
            "partially_delivered",
        )

        self.db.execute("BEGIN IMMEDIATE")
        first_invoice = quotations.invoice_sales_order(
            self.db, order_id, (base + timedelta(days=2)).isoformat(),
            (base + timedelta(days=30)).isoformat(), created_by=2,
        )
        self.db.commit()
        self.assertEqual(first_invoice["subtotal"], Decimal("60.00"))
        self.assertEqual(
            self.db.execute("SELECT status FROM sales_orders WHERE id=?", (order_id,)).fetchone()["status"],
            "partially_invoiced",
        )

        self.db.execute("BEGIN IMMEDIATE")
        second = quotations.create_delivery_note(
            self.db, order_id, (base + timedelta(days=3)).isoformat(),
            [{"sales_order_line_id": stock_line_id, "quantity": "2"}], 2,
        )
        quotations.post_delivery_note(self.db, second["delivery_note_id"], posted_by=2)
        self.db.commit()
        # A later delivery must not erase the fact that the order has already been invoiced partially.
        self.assertEqual(
            self.db.execute("SELECT status FROM sales_orders WHERE id=?", (order_id,)).fetchone()["status"],
            "partially_invoiced",
        )

        self.db.execute("BEGIN IMMEDIATE")
        second_invoice = quotations.invoice_sales_order(
            self.db, order_id, (base + timedelta(days=4)).isoformat(),
            (base + timedelta(days=30)).isoformat(), created_by=2,
        )
        self.db.commit()
        self.assertEqual(second_invoice["subtotal"], Decimal("40.00"))
        self.assertEqual(
            self.db.execute("SELECT status FROM sales_orders WHERE id=?", (order_id,)).fetchone()["status"],
            "invoiced",
        )
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM stock_movements WHERE movement_type='issue'").fetchone()[0],
            2,
        )
        self.assertEqual(
            sum((Decimal(row["cost_total"]) for row in self.db.execute(
                "SELECT cost_total FROM sales_invoice_lines WHERE invoice_id IN (?,?)",
                (first_invoice["invoice_id"], second_invoice["invoice_id"]),
            ).fetchall()), Decimal("0")),
            Decimal("40.00"),
        )

    def test_delivery_post_rejects_mismatched_product_without_stock_or_gl_effect(self):
        _, order_id, line_id = self._create_accepted_quote_and_order("2")
        other = self.db.execute(
            "INSERT INTO stock_products(sku,name_ar) VALUES('P2','صنف آخر')"
        ).lastrowid
        quotations.approve_sales_order(self.db, order_id, approver_id=2)
        self.db.commit()
        self.db.execute("BEGIN IMMEDIATE")
        note = quotations.create_delivery_note(
            self.db, order_id, (date.today() + timedelta(days=1)).isoformat(),
            [{"sales_order_line_id": line_id, "quantity": "1"}], 2,
        )
        self.db.execute(
            "UPDATE delivery_note_lines SET product_id=? WHERE delivery_note_id=?",
            (other, note["delivery_note_id"]),
        )
        with self.assertRaisesRegex(ValueError, "لا يطابق الصنف المخزني"):
            quotations.post_delivery_note(self.db, note["delivery_note_id"], posted_by=2)
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM stock_movements WHERE movement_type='issue'").fetchone()[0],
            0,
        )
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0], 0)
        self.db.rollback()

    def test_sales_order_routes_enforce_permissions_and_render_read_only_actions(self):
        from flask import Flask, session
        from jinja2 import ChoiceLoader, DictLoader

        quote_id, order_id, _line_id = self._create_accepted_quote_and_order("2")
        app = Flask(
            __name__,
            template_folder=str(Path(__file__).resolve().parents[1] / "templates"),
        )
        app.secret_key = "route-test-secret"
        app.add_url_rule("/login", endpoint="login", view_func=lambda: "")
        app.add_url_rule("/dashboard", endpoint="dashboard", view_func=lambda: "")
        app.jinja_loader = ChoiceLoader([
            DictLoader({"base.html": "{% block title %}{% endblock %}{% block content %}{% endblock %}"}),
            app.jinja_loader,
        ])
        app.jinja_env.globals["csrf_token"] = lambda: "test-token"
        app.context_processor(
            lambda: {"permissions": set(session.get("permissions", []))}
        )
        app.register_blueprint(quotations.quotes_bp)
        client = app.test_client()

        with client.session_transaction() as user_session:
            user_session["user_id"] = 1
            user_session["role"] = "accountant"
            user_session["permissions"] = ["sales.view", "sales.manage", "quotes.view"]

        quote_page = client.get(f"/sales/quotations/{quote_id}")
        self.assertEqual(quote_page.status_code, 200)
        quote_html = quote_page.get_data(as_text=True)
        self.assertNotIn("قبول العرض", quote_html)
        self.assertNotIn("إنشاء أمر بيع وإرساله للاعتماد", quote_html)

        order_page = client.get(f"/sales/quotations/orders/{order_id}")
        self.assertEqual(order_page.status_code, 200)
        self.assertIn("في انتظار الاعتماد", order_page.get_data(as_text=True))
        self.assertNotIn("اعتماد أمر البيع", order_page.get_data(as_text=True))
        denied = client.post(f"/sales/quotations/orders/{order_id}/approve")
        self.assertEqual(denied.status_code, 302)
        self.assertEqual(
            self.db.execute("SELECT status FROM sales_orders WHERE id=?", (order_id,)).fetchone()["status"],
            "pending_approval",
        )

        with client.session_transaction() as user_session:
            user_session["user_id"] = 2
            user_session["role"] = "admin"
            user_session["permissions"] = ["sales.view", "sales.manage", "sales.approve", "quotes.view", "quotes.manage"]
        approved = client.post(f"/sales/quotations/orders/{order_id}/approve")
        self.assertEqual(approved.status_code, 302)
        self.assertEqual(
            self.db.execute("SELECT status FROM sales_orders WHERE id=?", (order_id,)).fetchone()["status"],
            "approved",
        )
        approved_page = client.get(f"/sales/quotations/orders/{order_id}")
        self.assertIn("معتمد", approved_page.get_data(as_text=True))
        self.assertIn("إنشاء إذن تسليم جديد", approved_page.get_data(as_text=True))


    def test_sales_order_dimensions_flow_to_delivery_cogs_and_invoice(self):
        from datetime import date, timedelta
        branch=self.db.execute("INSERT INTO branches(code,name_ar) VALUES('B1','فرع القاهرة')").lastrowid
        center=self.db.execute("INSERT INTO cost_centers(code,name_ar) VALUES('CC1','مركز تجاري')").lastrowid
        project=self.db.execute("INSERT INTO projects(code,name_ar) VALUES('PJ1','مشروع تجريبي')").lastrowid
        salesperson=self.db.execute("INSERT INTO salespersons(code,name) VALUES('SP1','مندوب')").lastrowid
        quote_id,old_order,_=self._create_accepted_quote_and_order("1")
        self.db.execute("UPDATE quotations SET sales_order_id=NULL WHERE id=?",(quote_id,))
        self.db.execute("DELETE FROM sales_orders WHERE id=?",(old_order,)); self.db.commit()
        self.db.execute("BEGIN IMMEDIATE")
        order=quotations.create_sales_order_from_quotation(self.db,quote_id,1,self.warehouse_id,
            (date.today()+timedelta(days=10)).isoformat(),{"branch_id":branch,"cost_center_id":center,"project_id":project,"salesperson_id":salesperson})
        oid=order["sales_order_id"]
        line=self.db.execute("SELECT id FROM sales_order_lines WHERE sales_order_id=?",(oid,)).fetchone()["id"]
        quotations.approve_sales_order(self.db,oid,2); self.db.commit()
        self.db.execute("BEGIN IMMEDIATE")
        note=quotations.create_delivery_note(self.db,oid,(date.today()+timedelta(days=1)).isoformat(),[{"sales_order_line_id":line,"quantity":"1"}],2)
        quotations.post_delivery_note(self.db,note["delivery_note_id"],2); self.db.commit()
        row=self.db.execute("""SELECT branch_id,cost_center_id,project_id,customer_id,salesperson_id FROM journal_lines
            WHERE entry_id=(SELECT journal_entry_id FROM stock_movements WHERE movement_type='issue' ORDER BY id DESC LIMIT 1)
            AND debit<>'0'""").fetchone()
        self.assertEqual(tuple(row),(branch,center,project,self.customer_id,salesperson))
        self.db.execute("BEGIN IMMEDIATE")
        invoice=quotations.invoice_sales_order(self.db,oid,(date.today()+timedelta(days=2)).isoformat(),(date.today()+timedelta(days=30)).isoformat(),2)
        self.db.commit()
        row=self.db.execute("SELECT branch_id,cost_center_id,project_id,customer_id,salesperson_id FROM journal_lines WHERE entry_id=? AND debit<>'0'",(invoice["journal_entry_id"],)).fetchone()
        self.assertEqual(tuple(row),(branch,center,project,self.customer_id,salesperson))


    def test_sales_credit_note_converts_package_quantity_and_preserves_entered_unit(self):
        base_unit = self.db.execute(
            "SELECT id FROM stock_uoms WHERE code='PCS'"
        ).fetchone()["id"]
        self.db.execute(
            "UPDATE stock_products SET base_uom_id=? WHERE id=?",
            (base_unit, self.product_id),
        )
        package_id = self.db.execute(
            """INSERT INTO stock_product_uoms(
                 product_id,custom_code,custom_name,factor_to_base,is_base,
                 purchase_enabled,sales_enabled)
               VALUES(?,?,?,?,0,1,1)""",
            (self.product_id, "PACK2", "عبوة 2 قطعة", "2"),
        ).lastrowid
        extra_move = self.db.execute(
            """INSERT INTO stock_movements(
                 movement_no,movement_type,product_id,target_warehouse_id,quantity,
                 unit_cost,total_cost,movement_date)
               VALUES('OPEN-EXTRA','receipt',?,?, '5','10','50','2026-11-10')""",
            (self.product_id, self.warehouse_id),
        ).lastrowid
        self.db.execute(
            """INSERT INTO stock_valuation_layers(
                 product_id,warehouse_id,source_movement_id,qty_in,qty_remaining,unit_cost)
               VALUES(?,?,?,'5','5','10')""",
            (self.product_id, self.warehouse_id, extra_move),
        )
        self.db.commit()

        self.db.execute("BEGIN IMMEDIATE")
        invoice = sales.post_invoice(
            self.db, self.customer_id, "2026-11-10", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "2", "unit_price": "40",
              "uom_id": "P:" + str(package_id)}],
            "0", 1,
        )
        line_id = self.db.execute(
            "SELECT id FROM sales_invoice_lines WHERE invoice_id=?",
            (invoice["invoice_id"],),
        ).fetchone()["id"]
        first = sales.post_credit_note(
            self.db, invoice["invoice_id"], "2026-11-11", "مرتجع عبوة أولى",
            [{"invoice_line_id": line_id, "quantity": "1", "uom_key": "P:" + str(package_id)}], 1,
        )
        saved = self.db.execute(
            """SELECT quantity,entered_quantity,uom_key,entered_uom_name,conversion_factor
               FROM sales_credit_note_lines WHERE id=?""",
            (self.db.execute(
                "SELECT id FROM sales_credit_note_lines WHERE credit_note_id=?",
                (first["credit_note_id"],),
            ).fetchone()["id"],),
        ).fetchone()
        movement = self.db.execute(
            """SELECT quantity,entered_quantity,entered_uom_key,entered_uom_name
               FROM stock_movements WHERE movement_no LIKE 'SCN-%' ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        self.assertEqual(Decimal(saved["quantity"]), Decimal("2"))
        self.assertEqual(Decimal(saved["entered_quantity"]), Decimal("1"))
        self.assertEqual(saved["uom_key"], "P:" + str(package_id))
        self.assertEqual(saved["entered_uom_name"], "عبوة 2 قطعة")
        self.assertEqual(Decimal(saved["conversion_factor"]), Decimal("2"))
        self.assertEqual(Decimal(movement["quantity"]), Decimal("2"))
        self.assertEqual(Decimal(movement["entered_quantity"]), Decimal("1"))
        self.assertEqual(movement["entered_uom_key"], "P:" + str(package_id))

        second = sales.post_credit_note(
            self.db, invoice["invoice_id"], "2026-11-12", "استكمال المرتجع",
            [{"invoice_line_id": line_id, "quantity": "1", "uom_key": "P:" + str(package_id)}], 1,
        )
        self.db.commit()
        self.assertEqual(first["subtotal"] + second["subtotal"], invoice["subtotal"])
        returned_base_qty = sum(
            (Decimal(row["quantity"]) for row in self.db.execute(
                "SELECT quantity FROM sales_credit_note_lines WHERE invoice_line_id=?", (line_id,)
            )),
            Decimal("0"),
        )
        self.assertEqual(returned_base_qty, Decimal("4"))

    def test_sales_credit_note_rejects_package_return_exceeding_remaining_base_quantity(self):
        base_unit = self.db.execute(
            "SELECT id FROM stock_uoms WHERE code='PCS'"
        ).fetchone()["id"]
        self.db.execute(
            "UPDATE stock_products SET base_uom_id=? WHERE id=?",
            (base_unit, self.product_id),
        )
        package_id = self.db.execute(
            """INSERT INTO stock_product_uoms(
                 product_id,custom_code,custom_name,factor_to_base,is_base,
                 purchase_enabled,sales_enabled)
               VALUES(?,?,?,?,0,1,1)""",
            (self.product_id, "PACK3", "عبوة 3 قطع", "3"),
        ).lastrowid
        extra_move = self.db.execute(
            """INSERT INTO stock_movements(
                 movement_no,movement_type,product_id,target_warehouse_id,quantity,
                 unit_cost,total_cost,movement_date)
               VALUES('OPEN-EXTRA-2','receipt',?,?, '10','10','100','2026-11-10')""",
            (self.product_id, self.warehouse_id),
        ).lastrowid
        self.db.execute(
            """INSERT INTO stock_valuation_layers(
                 product_id,warehouse_id,source_movement_id,qty_in,qty_remaining,unit_cost)
               VALUES(?,?,?,'10','10','10')""",
            (self.product_id, self.warehouse_id, extra_move),
        )
        self.db.commit()

        self.db.execute("BEGIN IMMEDIATE")
        invoice = sales.post_invoice(
            self.db, self.customer_id, "2026-11-10", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "2", "unit_price": "60",
              "uom_id": "P:" + str(package_id)}],
            "0", 1,
        )
        line_id = self.db.execute(
            "SELECT id FROM sales_invoice_lines WHERE invoice_id=?", (invoice["invoice_id"],)
        ).fetchone()["id"]
        sales.post_credit_note(
            self.db, invoice["invoice_id"], "2026-11-11", "مرتجع جزئي",
            [{"invoice_line_id": line_id, "quantity": "1", "uom_key": "P:" + str(package_id)}], 1,
        )
        with self.assertRaisesRegex(ValueError, "الكمية المطلوبة للإشعار أكبر"):
            sales.post_credit_note(
                self.db, invoice["invoice_id"], "2026-11-12", "كمية زائدة",
                [{"invoice_line_id": line_id, "quantity": "2", "uom_key": "P:" + str(package_id)}], 1,
            )
        self.db.rollback()


    def test_sales_return_restores_original_quantities_across_multiple_lots(self):
        extra_move = self.db.execute(
            """INSERT INTO stock_movements(
                 movement_no,movement_type,product_id,target_warehouse_id,quantity,
                 unit_cost,total_cost,movement_date)
               VALUES('OPEN-LOT-2','receipt',?,?, '5','20','100','2026-11-10')""",
            (self.product_id, self.warehouse_id),
        ).lastrowid
        self.db.execute(
            """INSERT INTO stock_valuation_layers(
                 product_id,warehouse_id,source_movement_id,qty_in,qty_remaining,unit_cost)
               VALUES(?,?,?,'5','5','20')""",
            (self.product_id, self.warehouse_id, extra_move),
        )
        self.db.commit()

        self.db.execute("BEGIN IMMEDIATE")
        invoice = sales.post_invoice(
            self.db, self.customer_id, "2026-11-10", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "6", "unit_price": "30"}],
            "0", 1,
        )
        line_id = self.db.execute(
            "SELECT id FROM sales_invoice_lines WHERE invoice_id=?", (invoice["invoice_id"],)
        ).fetchone()["id"]
        original_lots = self.db.execute(
            "SELECT lot_id,quantity,cost_total FROM sales_invoice_line_lots WHERE invoice_line_id=? ORDER BY id",
            (line_id,),
        ).fetchall()
        self.assertEqual(len(original_lots), 2)
        self.assertEqual([Decimal(row["quantity"]) for row in original_lots],
                         [Decimal("5"), Decimal("1")])

        credit = sales.post_credit_note(
            self.db, invoice["invoice_id"], "2026-11-11", "مرتجع من أكثر من تشغيلة",
            [{"invoice_line_id": line_id, "quantity": "6"}], 1,
        )
        self.db.commit()
        returned_lots = self.db.execute(
            """SELECT lot_id,quantity FROM sales_credit_note_line_lots
               WHERE credit_note_line_id=(SELECT id FROM sales_credit_note_lines WHERE credit_note_id=?)
               ORDER BY id""",
            (credit["credit_note_id"],),
        ).fetchall()
        self.assertEqual([(row["lot_id"], Decimal(row["quantity"])) for row in returned_lots],
                         [(row["lot_id"], Decimal(row["quantity"])) for row in original_lots])
        movements = self.db.execute(
            """SELECT lot_id,quantity FROM stock_movements
               WHERE movement_no LIKE 'SCN-%' ORDER BY id"""
        ).fetchall()
        self.assertEqual([(row["lot_id"], Decimal(row["quantity"])) for row in movements],
                         [(row["lot_id"], Decimal(row["quantity"])) for row in original_lots])
        expected_return_cost = sum(
            (Decimal(row["cost_total"]) for row in original_lots), Decimal("0")
        )
        self.assertEqual(credit["total_cost"], expected_return_cost)

    def test_purchase_payment_sales_collection_return_reconcile_financial_statements(self):
        import purchases
        from unittest.mock import patch
        from flask import Flask

        # Reuse this test's isolated database so purchasing, sales, stock and GL
        # are validated as one business cycle.
        purchases.DB_PATH = sales.DB_PATH
        purchases.init_purchase_schema()
        self.db.executemany(
            "INSERT INTO accounts(code,name_ar,account_type) VALUES(?,?,?)",
            [
                ("2110", "الموردون", "liability"),
                ("2190", "بضاعة مستلمة غير مفوترة", "liability"),
                ("3000", "حقوق الملكية", "equity"),
            ],
        )
        self.db.commit()

        opening_entry_id = self.db.execute(
            """INSERT INTO journal_entries(entry_date,reference,description,status,created_by,created_at)
               VALUES('2026-11-10','OPEN-STOCK-PURCHASE-SALES','إثبات المخزون الافتتاحي','posted',1,CURRENT_TIMESTAMP)"""
        ).lastrowid
        self.db.executemany(
            "INSERT INTO journal_lines(entry_id,account_id,debit,credit,memo) VALUES(?,?,?,?,?)",
            [
                (opening_entry_id, self.db.execute("SELECT id FROM accounts WHERE code='1150'").fetchone()["id"], "50", "0", "مخزون أول المدة"),
                (opening_entry_id, self.db.execute("SELECT id FROM accounts WHERE code='3000'").fetchone()["id"], "0", "50", "حقوق الملكية"),
            ],
        )

        supplier_id = self.db.execute(
            "INSERT INTO suppliers(code,name) VALUES('SUP-E2E','مورد الدورة المتكاملة')"
        ).lastrowid
        po_id = self.db.execute(
            """INSERT INTO purchase_orders(po_no,supplier_id,order_date,status,created_by)
               VALUES('PO-E2E',?,'2026-11-15','approved',1)""",
            (supplier_id,),
        ).lastrowid
        self.db.execute(
            "INSERT INTO purchase_order_lines(po_id,product_id,quantity,unit_price) VALUES(?,?, '2','8')",
            (po_id, self.product_id),
        )
        self.db.commit()

        purchase_app = Flask(__name__)
        purchase_app.secret_key = "test"
        purchase_app.add_url_rule("/__test_global_search", endpoint="global_search", view_func=lambda: "")
        purchase_app.register_blueprint(purchases.purchase_bp)
        purchase_client = purchase_app.test_client()
        with purchase_client.session_transaction() as session:
            session["user_id"] = 1
            session["role"] = "admin"
        receipt_response = purchase_client.post("/purchases/receipts/new", data={
            "supplier_id": str(supplier_id),
            "product_id": str(self.product_id),
            "warehouse_id": str(self.warehouse_id),
            "po_id": str(po_id),
            "receipt_date": "2026-11-15",
            "quantity": "2",
            "unit_cost": "8",
        })
        self.assertEqual(receipt_response.status_code, 302)
        receipt = self.db.execute(
            "SELECT id,total_value,journal_entry_id FROM goods_receipts WHERE po_id=?",
            (po_id,),
        ).fetchone()
        self.assertEqual(Decimal(receipt["total_value"]), Decimal("16.00"))
        self.assertTrue(receipt["journal_entry_id"])

        bill_id = self.db.execute(
            """INSERT INTO supplier_bills(
                bill_no,supplier_id,bill_date,due_date,supplier_invoice_no,
                subtotal,tax_rate,tax_amount,total_amount,created_by,status,bill_kind
            ) VALUES('BILL-E2E',?,'2026-11-15','2026-12-15','INV-E2E',
                     '16','0','0','16',1,'approved','inventory')""",
            (supplier_id,),
        ).lastrowid
        self.db.execute(
            "INSERT INTO supplier_bill_receipts(bill_id,receipt_id,matched_amount) VALUES(?,?, '16')",
            (bill_id, receipt["id"]),
        )
        self.db.commit()
        with patch("purchases._activity_posting_accounts", return_value={
            "inventory": "1150", "cogs": "5100", "supplier_ap": "2110",
            "purchase_inventory": "1150",
        }):
            bill_entry_id = purchases.post_supplier_bill(self.db, bill_id, 1)
        self.db.commit()
        bill_lines = self.db.execute(
            """SELECT a.code,jl.debit,jl.credit FROM journal_lines jl
               JOIN accounts a ON a.id=jl.account_id WHERE jl.entry_id=?""",
            (bill_entry_id,),
        ).fetchall()
        bill_postings = {
            row["code"]: (Decimal(row["debit"]), Decimal(row["credit"]))
            for row in bill_lines
        }
        self.assertEqual(bill_postings["2190"], (Decimal("16.00"), Decimal("0.00")))
        self.assertEqual(bill_postings["2110"], (Decimal("0.00"), Decimal("16.00")))

        with patch("purchases._activity_posting_accounts", return_value={
            "inventory": "1150", "cogs": "5100", "supplier_ap": "2110",
            "purchase_inventory": "1150",
        }):
            payment_response = purchase_client.post("/purchases/payments/new", data={
                "supplier_id": str(supplier_id),
                "amount": "16",
                "cash_account_id": str(self.db.execute(
                    "SELECT id FROM accounts WHERE code='1110'"
                ).fetchone()["id"]),
                "payment_date": "2026-11-15",
                "reference": "SUPPLIER-E2E-PAYMENT",
            })
        self.assertEqual(payment_response.status_code, 302)
        supplier_bill = self.db.execute(
            "SELECT paid_amount,status FROM supplier_bills WHERE id=?", (bill_id,)
        ).fetchone()
        self.assertEqual(Decimal(supplier_bill["paid_amount"]), Decimal("16.00"))
        self.assertEqual(supplier_bill["status"], "posted")

        # Complete the real sales, collection, partial return and refund flow.
        self.db.execute("BEGIN IMMEDIATE")
        invoice = sales.post_invoice(
            self.db, self.customer_id, "2026-11-16", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "2", "unit_price": "20",
              "tax_rate": "14", "tax_treatment": "standard"}], "14", 1,
        )
        self.db.commit()
        cash_id = self.db.execute(
            "SELECT id FROM accounts WHERE code='1110'"
        ).fetchone()["id"]
        self.db.execute("BEGIN IMMEDIATE")
        sales.post_customer_receipt(
            self.db, invoice["invoice_id"], "2026-11-17", "45.60", cash_id, 1,
            "CUSTOMER-E2E-RECEIPT",
        )
        line_id = self.db.execute(
            "SELECT id FROM sales_invoice_lines WHERE invoice_id=?",
            (invoice["invoice_id"],),
        ).fetchone()["id"]
        credit = sales.post_credit_note(
            self.db, invoice["invoice_id"], "2026-11-18", "مرتجع جزئي",
            [{"invoice_line_id": line_id, "quantity": "1"}], 1,
        )
        refund = sales.post_customer_refund(
            self.db, credit["credit_note_id"], "2026-11-19", "22.80", cash_id, 1,
        )
        self.db.commit()
        self.assertEqual(credit["total_amount"], Decimal("22.80"))
        self.assertEqual(refund["amount"], Decimal("22.80"))

        # The supplier and customer subledgers must both be settled.
        supplier_ap_balance = self.db.execute(
            """SELECT COALESCE(SUM(CAST(jl.credit AS NUMERIC)-CAST(jl.debit AS NUMERIC)),0)
               FROM journal_lines jl JOIN accounts a ON a.id=jl.account_id
               WHERE a.code='2110'"""
        ).fetchone()[0]
        self.assertEqual(Decimal(str(supplier_ap_balance)), Decimal("0"))
        customer_after = self.db.execute(
            "SELECT paid_amount,credited_amount FROM sales_invoices WHERE id=?",
            (invoice["invoice_id"],),
        ).fetchone()
        self.assertEqual(Decimal(customer_after["paid_amount"]), Decimal("45.60"))
        self.assertEqual(Decimal(customer_after["credited_amount"]), Decimal("22.80"))

        # The customer control account must be fully settled after the
        # collection, credit note and refund; output VAT must retain only the
        # tax on the unreturned half of the invoice.
        customer_ar_balance = self.db.execute(
            """SELECT COALESCE(SUM(CAST(jl.debit AS NUMERIC)-CAST(jl.credit AS NUMERIC)),0)
               FROM journal_lines jl JOIN accounts a ON a.id=jl.account_id
               WHERE a.code='1130'"""
        ).fetchone()[0]
        output_vat_balance = self.db.execute(
            """SELECT COALESCE(SUM(CAST(jl.credit AS NUMERIC)-CAST(jl.debit AS NUMERIC)),0)
               FROM journal_lines jl JOIN accounts a ON a.id=jl.account_id
               WHERE a.code='2130'"""
        ).fetchone()[0]
        self.assertEqual(Decimal(str(customer_ar_balance)), Decimal("0"))
        self.assertEqual(Decimal(str(output_vat_balance)), Decimal("2.80"))

        # Use the stock engine's authoritative remaining layer value. Under
        # weighted-average costing, unit_cost is the receipt cost and is not
        # rewritten when part of a layer is consumed; qty * unit_cost would
        # therefore overstate or understate the remaining valuation.
        stock.initialize_stock_schema()
        layer_value = sum((
            Decimal(row["value_remaining"])
            for row in self.db.execute(
                "SELECT value_remaining FROM stock_valuation_layers WHERE product_id=?",
                (self.product_id,),
            )
        ), Decimal("0"))
        inventory_gl = Decimal(str(self.db.execute(
            """SELECT COALESCE(SUM(CAST(jl.debit AS NUMERIC)-CAST(jl.credit AS NUMERIC)),0)
               FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
               JOIN accounts a ON a.id=jl.account_id
               WHERE je.status='posted' AND a.code='1150'"""
        ).fetchone()[0]))
        self.assertEqual(inventory_gl, layer_value)

        # Real statement builders: purchase cost is capitalized, sale margin
        # is recognized, and the balance sheet remains in balance.
        self.db.execute("ALTER TABLE accounts ADD COLUMN statement_section TEXT")
        self.db.execute(
            "UPDATE accounts SET statement_section='direct_costs' WHERE code='5100'"
        )
        self.db.commit()
        from income_statement import build_income_statement
        from financial_planning import build_balance_sheet_snapshot
        income = build_income_statement(self.db, "2026-11-10", "2026-11-30")
        self.assertEqual(income["revenue_total"], Decimal("20.00"))
        self.assertEqual(income["cogs_total"], invoice["total_cost"] / 2)
        self.assertEqual(income["gross_profit"], income["revenue_total"] - income["cogs_total"])
        self.assertEqual(income["net_profit"], income["gross_profit"])

        position = build_balance_sheet_snapshot(self.db, "2026-11-30")
        self.assertEqual(position["balance_difference"], Decimal("0.00"))
        self.assertTrue(position["balanced"])

        # Exercise the real IAS 7 cash-flow builder against actual cash journals.
        from management_dashboard import initialize_management_dashboard_schema
        from financial_planning import build_cash_flow_snapshot
        initialize_management_dashboard_schema(self.db, "commercial")
        cash_flow = build_cash_flow_snapshot(self.db, "2026-11-10", "2026-11-30")
        self.assertTrue(cash_flow["cash_mapping_available"])
        self.assertTrue(cash_flow["cash_reconciles"])
        self.assertEqual(cash_flow["opening"], Decimal("0"))
        self.assertEqual(cash_flow["closing"], Decimal("6.80"))
        self.assertEqual(cash_flow["net_change"], Decimal("6.80"))
        self.assertEqual(cash_flow["reconciliation_difference"], Decimal("0"))
        self.assertEqual(cash_flow["sections"]["operating"]["in"], Decimal("45.60"))
        self.assertEqual(cash_flow["sections"]["operating"]["out"], Decimal("38.80"))

        # Trial balance must balance across the complete integrated cycle.
        trial_balance = self.db.execute(
            """SELECT COALESCE(SUM(CAST(jl.debit AS NUMERIC)),0) debit,
                      COALESCE(SUM(CAST(jl.credit AS NUMERIC)),0) credit
               FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
               WHERE je.status='posted'"""
        ).fetchone()
        self.assertEqual(
            Decimal(str(trial_balance["debit"])),
            Decimal(str(trial_balance["credit"])),
        )

        # Every posted journal entry in the full procure-to-sale cycle balances.
        unbalanced = self.db.execute(
            """SELECT je.id FROM journal_entries je JOIN journal_lines jl ON jl.entry_id=je.id
               WHERE je.status='posted' GROUP BY je.id
               HAVING ABS(SUM(CAST(jl.debit AS NUMERIC))-SUM(CAST(jl.credit AS NUMERIC)))>0.005"""
        ).fetchall()
        self.assertEqual(unbalanced, [])

if __name__ == "__main__":
    unittest.main()
