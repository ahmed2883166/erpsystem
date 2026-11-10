import tempfile
import unittest
from pathlib import Path
from decimal import Decimal

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
          debit REAL DEFAULT 0, credit REAL DEFAULT 0, memo TEXT
        );
        CREATE TABLE company_settings(setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL);
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

    def test_insufficient_stock_rolls_back_invoice(self):
        self.db.execute("BEGIN IMMEDIATE")
        with self.assertRaisesRegex(ValueError, "الرصيد غير كافٍ"):
            sales.post_invoice(self.db, self.customer_id, "2026-11-10", self.warehouse_id,
                [{"product_id": self.product_id, "quantity": "99", "unit_price": "20"}], "0", 1)
        self.db.rollback()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM sales_invoices").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
