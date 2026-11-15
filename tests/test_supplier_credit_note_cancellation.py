import sqlite3
import unittest
from unittest.mock import patch

import purchases


class SupplierCreditNoteCancellationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE journal_entries(
                id INTEGER PRIMARY KEY, status TEXT NOT NULL, entry_date TEXT NOT NULL
            );
            CREATE TABLE journal_lines(
                id INTEGER PRIMARY KEY, entry_id INTEGER NOT NULL, account_id INTEGER NOT NULL,
                debit TEXT NOT NULL, credit TEXT NOT NULL, memo TEXT,
                supplier_id INTEGER, stock_product_id INTEGER, stock_warehouse_id INTEGER
            );
            CREATE TABLE supplier_bills(
                id INTEGER PRIMARY KEY, credited_amount TEXT NOT NULL
            );
            CREATE TABLE supplier_credit_notes(
                id INTEGER PRIMARY KEY, credit_no TEXT NOT NULL, supplier_id INTEGER NOT NULL,
                bill_id INTEGER NOT NULL, credit_date TEXT NOT NULL, supplier_credit_no TEXT NOT NULL,
                total_amount TEXT NOT NULL, journal_entry_id INTEGER NOT NULL, status TEXT NOT NULL,
                cancel_journal_entry_id INTEGER, cancelled_by INTEGER, cancelled_at TEXT,
                cancellation_reason TEXT
            );
            CREATE TABLE supplier_credit_note_lines(
                id INTEGER PRIMARY KEY, credit_note_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
                warehouse_id INTEGER NOT NULL, quantity TEXT NOT NULL, total_cost TEXT NOT NULL
            );
            CREATE TABLE stock_products(id INTEGER PRIMARY KEY);
            CREATE TABLE stock_movements(
                id INTEGER PRIMARY KEY, movement_no TEXT UNIQUE NOT NULL, movement_type TEXT NOT NULL,
                product_id INTEGER NOT NULL, source_warehouse_id INTEGER, target_warehouse_id INTEGER,
                target_location_id INTEGER, lot_id INTEGER, quantity TEXT NOT NULL,
                unit_cost TEXT NOT NULL, total_cost TEXT NOT NULL, movement_date TEXT NOT NULL,
                notes TEXT NOT NULL, created_by INTEGER, journal_entry_id INTEGER, accounting_status TEXT
            );
            CREATE TABLE stock_valuation_layers(
                id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL, warehouse_id INTEGER NOT NULL,
                location_id INTEGER, lot_id INTEGER, source_movement_id INTEGER NOT NULL,
                qty_in TEXT NOT NULL, qty_remaining TEXT NOT NULL, unit_cost TEXT NOT NULL,
                value_remaining TEXT NOT NULL
            );
            CREATE TABLE supplier_credit_note_layer_allocations(
                id INTEGER PRIMARY KEY, credit_note_line_id INTEGER NOT NULL,
                stock_movement_id INTEGER NOT NULL, source_layer_id INTEGER NOT NULL,
                lot_id INTEGER, location_id INTEGER, quantity TEXT NOT NULL,
                unit_cost TEXT NOT NULL, total_cost TEXT NOT NULL
            );
        """)
        self.conn.execute("INSERT INTO journal_entries VALUES(10,'posted','2026-05-10')")
        self.conn.execute(
            "INSERT INTO journal_lines VALUES(1,10,200,'57','0','إشعار مورد',8,1,10)"
        )
        self.conn.execute("INSERT INTO supplier_bills VALUES(20,'57')")
        self.conn.execute(
            """INSERT INTO supplier_credit_notes(
                id,credit_no,supplier_id,bill_id,credit_date,supplier_credit_no,
                total_amount,journal_entry_id,status
            ) VALUES(30,'SCN-30',8,20,'2026-05-10','SUP-CN-30','57',10,'posted')"""
        )
        self.conn.execute("INSERT INTO stock_products VALUES(1)")
        self.conn.execute(
            """INSERT INTO supplier_credit_note_lines(
                id,credit_note_id,product_id,warehouse_id,quantity,total_cost
            ) VALUES(40,30,1,10,'2.5','37.5')"""
        )
        self.conn.execute(
            """INSERT INTO stock_movements(
                id,movement_no,movement_type,product_id,source_warehouse_id,lot_id,
                quantity,unit_cost,total_cost,movement_date,notes,created_by,journal_entry_id,accounting_status
            ) VALUES(50,'SUPRET-50','issue',1,10,7,'2.5','15','37.5',
                     '2026-05-10','مرتجع مورد',1,10,'posted')"""
        )
        self.conn.execute(
            """INSERT INTO stock_valuation_layers(
                id,product_id,warehouse_id,location_id,lot_id,source_movement_id,
                qty_in,qty_remaining,unit_cost,value_remaining
            ) VALUES(60,1,10,3,7,5,'5','2.5','15','37.5')"""
        )
        self.conn.execute(
            """INSERT INTO supplier_credit_note_layer_allocations(
                id,credit_note_line_id,stock_movement_id,source_layer_id,lot_id,location_id,
                quantity,unit_cost,total_cost
            ) VALUES(70,40,50,60,7,3,'2.5','15','37.5')"""
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_cancel_restores_same_cost_lot_and_location_and_reverses_bill_credit(self):
        with patch.object(purchases, "post_journal", return_value=99) as post:
            result = purchases.cancel_supplier_credit_note(
                self.conn, 30, "2026-05-11", "إلغاء معتمد", 4
            )
        self.assertEqual(result["reversal_journal_id"], 99)
        post.assert_called_once()
        reversal_lines = post.call_args.args[4]
        self.assertEqual(reversal_lines[0][1], 0)
        self.assertEqual(reversal_lines[0][2], 57)
        movement = self.conn.execute(
            "SELECT * FROM stock_movements WHERE movement_no LIKE 'SUPCANCEL-%'"
        ).fetchone()
        self.assertEqual(movement["movement_type"], "receipt")
        self.assertEqual(movement["target_warehouse_id"], 10)
        self.assertEqual(movement["target_location_id"], 3)
        self.assertEqual(movement["lot_id"], 7)
        self.assertEqual(movement["quantity"], "2.5")
        self.assertEqual(movement["total_cost"], "37.5")
        layer = self.conn.execute(
            "SELECT * FROM stock_valuation_layers WHERE source_movement_id=?",
            (movement["id"],)
        ).fetchone()
        self.assertEqual(layer["qty_remaining"], "2.5")
        self.assertEqual(layer["value_remaining"], "37.5")
        original_move = self.conn.execute(
            "SELECT movement_type,accounting_status FROM stock_movements WHERE id=50"
        ).fetchone()
        self.assertEqual(tuple(original_move), ("cancelled_issue", "reversed"))
        self.assertEqual(
            self.conn.execute("SELECT credited_amount FROM supplier_bills WHERE id=20").fetchone()[0],
            "0.00"
        )
        note = self.conn.execute(
            "SELECT status,cancel_journal_entry_id,cancellation_reason FROM supplier_credit_notes WHERE id=30"
        ).fetchone()
        self.assertEqual(tuple(note), ("cancelled", 99, "إلغاء معتمد"))

    def test_second_cancellation_is_rejected(self):
        self.conn.execute("UPDATE supplier_credit_notes SET status='cancelled' WHERE id=30")
        with self.assertRaisesRegex(ValueError, "تم إلغاؤه بالفعل"):
            purchases.cancel_supplier_credit_note(
                self.conn, 30, "2026-05-11", "محاولة ثانية", 4
            )

    def test_legacy_note_without_layer_links_is_rejected(self):
        self.conn.execute("DELETE FROM supplier_credit_note_layer_allocations")
        with self.assertRaisesRegex(ValueError, "لا يحتوي على روابط كاملة"):
            purchases.cancel_supplier_credit_note(
                self.conn, 30, "2026-05-11", "إلغاء قديم", 4
            )

    def test_mismatched_layer_quantity_is_rejected_before_posting(self):
        self.conn.execute(
            "UPDATE supplier_credit_note_layer_allocations SET quantity='2' WHERE id=70"
        )
        with patch.object(purchases, "post_journal") as post:
            with self.assertRaisesRegex(ValueError, "لا تطابق كمية أو قيمة"):
                purchases.cancel_supplier_credit_note(
                    self.conn, 30, "2026-05-11", "بيانات غير متطابقة", 4
                )
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
