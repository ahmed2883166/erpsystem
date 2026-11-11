import tempfile
import unittest
from pathlib import Path
from decimal import Decimal

import analytic_dimensions
import sales
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
            ("1110", "الصندوق", "asset"), ("1130", "العملاء", "asset"),
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


if __name__ == "__main__":
    unittest.main()
