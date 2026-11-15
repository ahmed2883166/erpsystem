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
                id INTEGER PRIMARY KEY, status TEXT NOT NULL, entry_date TEXT NOT NULL,
                reference TEXT, description TEXT, created_by INTEGER, created_at TEXT
            );
            CREATE TABLE accounts(
                id INTEGER PRIMARY KEY, code TEXT, name_ar TEXT, account_type TEXT,
                is_group INTEGER DEFAULT 0, active INTEGER DEFAULT 1
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
        self.conn.executemany(
            "INSERT INTO accounts(id,code,name_ar,account_type,is_group,active) VALUES(?,?,?,?,0,1)",
            [(100,'2110','الموردون','liability'),(200,'1150','المخزون','asset'),
             (300,'1170','ضريبة مدخلات','asset')]
        )
        self.conn.execute(
            """INSERT INTO journal_entries(
                id,status,entry_date,reference,description,created_by,created_at
            ) VALUES(10,'posted','2026-05-10','SCN-30','إشعار مورد',1,'2026-05-10')"""
        )
        self.conn.executemany(
            """INSERT INTO journal_lines(
                id,entry_id,account_id,debit,credit,memo,supplier_id,stock_product_id,stock_warehouse_id
            ) VALUES(?,10,?,?,?,?,?,?,?)""",
            [(1,100,'92','0','إشعار مورد',8,None,None),
             (2,200,'0','52.5','مخزون مرتجع مخزن 1',8,1,10),
             (3,300,'0','19.5','ضريبة مدخلات',8,None,None),
             (4,200,'0','20','مخزون مرتجع مخزن 2',8,2,20)]
        )
        self.conn.execute("INSERT INTO supplier_bills VALUES(20,'92')")
        self.conn.execute(
            """INSERT INTO supplier_credit_notes(
                id,credit_no,supplier_id,bill_id,credit_date,supplier_credit_no,
                total_amount,journal_entry_id,status
            ) VALUES(30,'SCN-30',8,20,'2026-05-10','SUP-CN-30','92',10,'posted')"""
        )
        self.conn.executemany("INSERT INTO stock_products VALUES(?)", [(1,), (2,)])
        self.conn.execute(
            """INSERT INTO stock_movements(
                id,movement_no,movement_type,product_id,target_warehouse_id,quantity,
                unit_cost,total_cost,movement_date,notes,accounting_status
            ) VALUES(5,'PUR-5','receipt',1,10,'5','15','75',
                     '2026-05-01','استلام أصلي','posted')"""
        )
        self.conn.execute(
            """INSERT INTO supplier_credit_note_lines(
                id,credit_note_id,product_id,warehouse_id,quantity,total_cost
            ) VALUES(40,30,1,10,'3.5','52.5')"""
        )
        self.conn.execute(
            """INSERT INTO stock_movements(
                id,movement_no,movement_type,product_id,source_warehouse_id,lot_id,
                quantity,unit_cost,total_cost,movement_date,notes,created_by,journal_entry_id,accounting_status
            ) VALUES(50,'SUPRET-50','issue',1,10,7,'2.5','15','37.5',
                     '2026-05-10','مرتجع مورد',1,10,'posted')"""
        )
        self.conn.execute(
            """INSERT INTO stock_movements(
                id,movement_no,movement_type,product_id,target_warehouse_id,quantity,
                unit_cost,total_cost,movement_date,notes,accounting_status
            ) VALUES(6,'PUR-6','receipt',1,10,'5','15','75',
                     '2026-05-02','استلام أصلي 2','posted')"""
        )
        self.conn.execute(
            """INSERT INTO stock_movements(
                id,movement_no,movement_type,product_id,source_warehouse_id,lot_id,
                quantity,unit_cost,total_cost,movement_date,notes,created_by,journal_entry_id,accounting_status
            ) VALUES(51,'SUPRET-51','issue',1,10,8,'1','15','15',
                     '2026-05-10','مرتجع مورد تشغيلة 2',1,10,'posted')"""
        )
        self.conn.execute(
            """INSERT INTO stock_valuation_layers(
                id,product_id,warehouse_id,location_id,lot_id,source_movement_id,
                qty_in,qty_remaining,unit_cost,value_remaining
            ) VALUES(60,1,10,3,7,5,'5','2.5','15','37.5')"""
        )
        self.conn.execute(
            """INSERT INTO stock_valuation_layers(
                id,product_id,warehouse_id,location_id,lot_id,source_movement_id,
                qty_in,qty_remaining,unit_cost,value_remaining
            ) VALUES(61,1,10,4,8,6,'5','4','15','60')"""
        )
        self.conn.execute(
            """INSERT INTO supplier_credit_note_layer_allocations(
                id,credit_note_line_id,stock_movement_id,source_layer_id,lot_id,location_id,
                quantity,unit_cost,total_cost
            ) VALUES(70,40,50,60,7,3,'2.5','15','37.5'),
                     (71,40,51,61,8,4,'1','15','15')"""
        )
        self.conn.execute(
            """INSERT INTO supplier_credit_note_lines(
                id,credit_note_id,product_id,warehouse_id,quantity,total_cost
            ) VALUES(41,30,2,20,'1','20')"""
        )
        self.conn.execute(
            """INSERT INTO stock_movements(
                id,movement_no,movement_type,product_id,target_warehouse_id,quantity,
                unit_cost,total_cost,movement_date,notes,accounting_status
            ) VALUES(7,'PUR-7','receipt',2,20,'4','20','80',
                     '2026-05-03','استلام مخزن 2','posted')"""
        )
        self.conn.execute(
            """INSERT INTO stock_movements(
                id,movement_no,movement_type,product_id,source_warehouse_id,lot_id,
                quantity,unit_cost,total_cost,movement_date,notes,created_by,journal_entry_id,accounting_status
            ) VALUES(52,'SUPRET-52','issue',2,20,9,'1','20','20',
                     '2026-05-10','مرتجع مورد مخزن 2',1,10,'posted')"""
        )
        self.conn.execute(
            """INSERT INTO stock_valuation_layers(
                id,product_id,warehouse_id,location_id,lot_id,source_movement_id,
                qty_in,qty_remaining,unit_cost,value_remaining
            ) VALUES(62,2,20,5,9,7,'4','3','20','60')"""
        )
        self.conn.execute(
            """INSERT INTO supplier_credit_note_layer_allocations(
                id,credit_note_line_id,stock_movement_id,source_layer_id,lot_id,location_id,
                quantity,unit_cost,total_cost
            ) VALUES(72,41,52,62,9,5,'1','20','20')"""
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_cancel_restores_same_cost_lot_and_location_and_reverses_bill_credit(self):
        result = purchases.cancel_supplier_credit_note(
            self.conn, 30, "2026-05-11", "إلغاء معتمد", 4
        )
        self.assertEqual(result["reversal_journal_id"], 11)
        reversal_lines = self.conn.execute(
            "SELECT account_id,SUM(CAST(debit AS REAL)) debit,SUM(CAST(credit AS REAL)) credit FROM journal_lines WHERE entry_id=? GROUP BY account_id ORDER BY account_id",
            (result["reversal_journal_id"],)
        ).fetchall()
        self.assertEqual(
            [(row["account_id"], row["debit"], row["credit"]) for row in reversal_lines],
            [(100, "0.00", "92.00"), (200, "72.50", "0.00"), (300, "19.50", "0.00")]
        )
        restored = self.conn.execute(
            """SELECT m.movement_type,m.target_warehouse_id,m.target_location_id,m.lot_id,
                      m.quantity,m.total_cost,l.qty_remaining,l.value_remaining
               FROM stock_movements m JOIN stock_valuation_layers l ON l.source_movement_id=m.id
               WHERE m.movement_no LIKE 'SUPCANCEL-%' ORDER BY m.id"""
        ).fetchall()
        self.assertEqual(len(restored), 3)
        self.assertEqual(
            [(r["movement_type"],r["target_warehouse_id"],r["target_location_id"],r["lot_id"],
              r["quantity"],r["total_cost"],r["qty_remaining"],r["value_remaining"]) for r in restored],
            [("receipt",10,3,7,"2.5","37.5","2.5","37.5"),
             ("receipt",10,4,8,"1","15","1","15"),
             ("receipt",20,5,9,"1","20","1","20")]
        )
        original_move = self.conn.execute(
            "SELECT movement_type,accounting_status FROM stock_movements WHERE id=50"
        ).fetchone()
        self.assertEqual(tuple(original_move), ("issue", "posted"))
        stock_balance = self.conn.execute(
            """SELECT
                SUM(CASE WHEN movement_type='receipt' AND target_warehouse_id=10
                         THEN CAST(quantity AS REAL) ELSE 0 END)
                - SUM(CASE WHEN movement_type IN ('issue','scrap') AND source_warehouse_id=10
                           THEN CAST(quantity AS REAL) ELSE 0 END)
               FROM stock_movements WHERE product_id=1"""
        ).fetchone()[0]
        self.assertEqual(stock_balance, 10.0)
        second_warehouse_balance = self.conn.execute(
            """SELECT
                SUM(CASE WHEN movement_type='receipt' AND target_warehouse_id=20
                         THEN CAST(quantity AS REAL) ELSE 0 END)
                - SUM(CASE WHEN movement_type IN ('issue','scrap') AND source_warehouse_id=20
                           THEN CAST(quantity AS REAL) ELSE 0 END)
               FROM stock_movements WHERE product_id=2"""
        ).fetchone()[0]
        self.assertEqual(second_warehouse_balance, 4.0)
        self.assertEqual(
            self.conn.execute("SELECT credited_amount FROM supplier_bills WHERE id=20").fetchone()[0],
            "0.00"
        )
        note = self.conn.execute(
            "SELECT status,cancel_journal_entry_id,cancellation_reason FROM supplier_credit_notes WHERE id=30"
        ).fetchone()
        self.assertEqual(tuple(note), ("cancelled", 11, "إلغاء معتمد"))
        unbalanced = self.conn.execute(
            """SELECT entry_id,SUM(CAST(debit AS REAL))-SUM(CAST(credit AS REAL)) difference
               FROM journal_lines GROUP BY entry_id
               HAVING ABS(SUM(CAST(debit AS REAL))-SUM(CAST(credit AS REAL)))>0.005"""
        ).fetchall()
        self.assertEqual(unbalanced, [])

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
