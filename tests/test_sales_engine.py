import json
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal

import analytic_dimensions
import sales
import quotations
import stock


class SalesIntegrationTests(unittest.TestCase):
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
            ("4100", "المبيعات", "revenue"), ("5100", "تكلفة المبيعات", "expense")
        ]:
            self.db.execute("INSERT INTO accounts(code,name_ar,account_type) VALUES(?,?,?)", (code,name,typ))
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

    def test_invoice_posts_revenue_tax_cogs_and_stock_issue(self):
        self.db.execute("BEGIN IMMEDIATE")
        result = sales.post_invoice(self.db, self.customer_id, "2026-11-10", self.warehouse_id,
            [{"product_id": self.product_id, "quantity": "2", "unit_price": "20"}], "14", 1)
        self.db.commit()
        self.assertEqual(result["subtotal"], Decimal("40.00"))
        self.assertEqual(result["tax_amount"], Decimal("5.60"))
        self.assertEqual(result["total_amount"], Decimal("45.60"))
        self.assertEqual(result["total_cost"], Decimal("20.00"))
        remaining = self.db.execute("SELECT qty_remaining FROM stock_valuation_layers").fetchone()["qty_remaining"]
        self.assertEqual(Decimal(remaining), Decimal("3.0000"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0], 2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM stock_movements WHERE movement_type='issue'").fetchone()[0], 1)

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
            "SELECT account_id,debit,credit FROM journal_lines WHERE entry_id=?",
            (receipt["journal_entry_id"],),
        ).fetchall()
        cash_code = self.db.execute("SELECT code FROM accounts WHERE id=?", (journal[0]["account_id"],)).fetchone()["code"]
        ar_code = self.db.execute("SELECT code FROM accounts WHERE id=?", (journal[1]["account_id"],)).fetchone()["code"]
        self.assertEqual((cash_code, Decimal(journal[0]["debit"]), Decimal(journal[0]["credit"])),
                         ("1110", Decimal("70.00"), Decimal("0")))
        self.assertEqual((ar_code, Decimal(journal[1]["debit"]), Decimal(journal[1]["credit"])),
                         ("1130", Decimal("0"), Decimal("70.00")))

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
        self.assertEqual(amounts["4100"], (Decimal("20.00"), Decimal("0")))
        self.assertEqual(amounts["2130"], (Decimal("2.80"), Decimal("0")))
        self.assertEqual(amounts["1130"], (Decimal("0"), Decimal("22.80")))

        with self.assertRaisesRegex(ValueError, "الكمية المطلوبة"):
            self.db.execute("BEGIN IMMEDIATE")
            sales.post_credit_note(
                self.db, invoice["invoice_id"], "2026-11-12", "محاولة تكرار المرتجع",
                [{"invoice_line_id": line_id, "quantity": "2"}], 1
            )
        self.db.rollback()

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
        self.assertEqual(credit_totals["4100"][0], Decimal("180"))
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

    def test_credit_note_cancellation_posts_reversals_and_removes_returned_stock(self):
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
                         ("fixed", "15", "15.00"))

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
        actual = []
        for row in self.db.execute(
            """SELECT jl.entry_id,a.code,jl.debit,jl.credit
               FROM journal_lines jl JOIN accounts a ON a.id=jl.account_id
               WHERE jl.entry_id IN (?,?,?) ORDER BY jl.entry_id,a.code""",
            (advance["journal_entry_id"], allocation["journal_entry_id"], refund["journal_entry_id"]),
        ):
            actual.append((row["entry_id"], row["code"], Decimal(row["debit"]), Decimal(row["credit"])))
        self.assertEqual(actual, sorted(expected, key=lambda x: (x[0], x[1])))
        for entry_id in (advance["journal_entry_id"], allocation["journal_entry_id"], refund["journal_entry_id"]):
            totals = self.db.execute(
                "SELECT SUM(CAST(debit AS REAL)) debit,SUM(CAST(credit AS REAL)) credit FROM journal_lines WHERE entry_id=?",
                (entry_id,),
            ).fetchone()
            self.assertAlmostEqual(totals["debit"], totals["credit"], places=2)

otals["credit"], places=2)

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
            data={"invoice_id": str(invoice["invoice_id"]), "amount": "50",
                  "allocation_date": "2026-11-12"},
        )
        self.assertEqual(response.status_code, 302)
        paid = self.db.execute(
            "SELECT paid_amount FROM sales_invoices WHERE id=?", (invoice["invoice_id"],)
        ).fetchone()["paid_amount"]
        self.assertEqual(Decimal(paid), Decimal("50.00"))

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
        self.assertEqual(sales._customer_advance_available(self.db, advance["id"]), Decimal("15.00"))



    def _create_accepted_quote_and_order(self, quantity="4"):
        quotations.DB_PATH = sales.DB_PATH
        quotations.initialize_quotes_schema()
        cur = self.db.execute(
            """INSERT INTO quotations(quote_no,customer_id,quote_date,valid_until,status,
               subtotal,tax_rate,tax_amount,total_amount,notes,created_by)
               VALUES('QT-TEST-1',?,'2026-11-10','2026-12-10','accepted',
                      ?, '14', ?, ?, '', 1)""",
            (self.customer_id, str(Decimal(quantity) * Decimal("20")),
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

    def test_sales_order_partial_delivery_and_invoicing_never_issue_stock_twice(self):
        quote_id, order_id, order_line_id = self._create_accepted_quote_and_order("4")
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
            self.db, order_id, "2026-11-14",
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
        with self.assertRaisesRegex(ValueError, "لا يسبق تاريخ إذن التسليم"):
            quotations.invoice_sales_order(
                self.db, order_id, "2026-11-13", "2026-11-30", created_by=2,
            )
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM sales_invoices WHERE sales_order_id=?", (order_id,)
        ).fetchone()[0], 0)
        first_invoice = quotations.invoice_sales_order(
            self.db, order_id, "2026-11-15", "2026-11-30", created_by=2,
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
        credit = sales.post_credit_note(
            self.db, first_invoice["invoice_id"], "2026-11-16", "مرتجع من فاتورة أمر البيع",
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
            self.db, order_id, "2026-11-16",
            [{"sales_order_line_id": order_line_id, "quantity": "2"}], 2,
        )
        quotations.post_delivery_note(self.db, second_note["delivery_note_id"], posted_by=2)
        second_invoice = quotations.invoice_sales_order(
            self.db, order_id, "2026-11-17", "2026-11-30", created_by=2,
        )
        self.db.commit()

        self.assertEqual(second_invoice["total_cost"], Decimal("20.00"))
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM stock_movements WHERE movement_type='issue'"
        ).fetchone()[0], 2)
        self.assertEqual(
            Decimal(self.db.execute("SELECT qty_remaining FROM stock_valuation_layers").fetchone()[0]),
            Decimal("1.0000"),
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
        quotations.approve_sales_order(self.db, order_id, approver_id=2)
        self.db.commit()

        self.db.execute("BEGIN IMMEDIATE")
        with self.assertRaisesRegex(ValueError, "تتجاوز الكمية المتبقية"):
            quotations.create_delivery_note(
                self.db, order_id, "2026-11-14",
                [{"sales_order_line_id": order_line_id, "quantity": "7"}], 2,
            )
        self.db.rollback()
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM delivery_notes WHERE sales_order_id=?", (order_id,)
        ).fetchone()[0], 0)

        self.db.execute("BEGIN IMMEDIATE")
        note = quotations.create_delivery_note(
            self.db, order_id, "2026-11-14",
            [{"sales_order_line_id": order_line_id, "quantity": "6"}], 2,
        )
        with self.assertRaisesRegex(ValueError, "الرصيد غير كافٍ"):
            quotations.post_delivery_note(self.db, note["delivery_note_id"], posted_by=2)
        self.db.rollback()
        self.assertEqual(
            Decimal(self.db.execute("SELECT qty_remaining FROM stock_valuation_layers").fetchone()[0]),
            Decimal("5"),
        )
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM stock_movements WHERE movement_type='issue'"
        ).fetchone()[0], 0)



if __name__ == "__main__":
    unittest.main()
