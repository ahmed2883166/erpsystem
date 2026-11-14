import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch
from pathlib import Path
import stock


class StockValuationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        stock.DB_PATH = Path(self.tmp.name) / "test.db"
        stock.initialize_stock_schema()
        self.db = stock.get_db()
        self.db.execute("INSERT INTO stock_products(sku,name_ar) VALUES('P1','منتج اختبار')")
        self.product_id = self.db.execute("SELECT id FROM stock_products").fetchone()["id"]
        self.db.execute("INSERT INTO stock_warehouses(code,name_ar) VALUES('W1','مخزن اختبار')")
        self.warehouse_id = self.db.execute("SELECT id FROM stock_warehouses").fetchone()["id"]
        self.db.execute("""INSERT INTO stock_movements(movement_no,movement_type,product_id,target_warehouse_id,quantity,unit_cost,total_cost,movement_date)
                           VALUES('R1','receipt',?,?, '100','10','1000','2026-01-01')""",
                        (self.product_id,self.warehouse_id))
        movement_id = self.db.execute("SELECT id FROM stock_movements WHERE movement_no='R1'").fetchone()["id"]
        self.db.execute("""INSERT INTO stock_valuation_layers(product_id,warehouse_id,source_movement_id,qty_in,qty_remaining,unit_cost)
                           VALUES(?,?,?,'100','100','10')""",(self.product_id,self.warehouse_id,movement_id))
        self.db.execute("""INSERT INTO stock_movements(movement_no,movement_type,product_id,target_warehouse_id,quantity,unit_cost,total_cost,movement_date)
                           VALUES('R2','receipt',?,?, '100','14','1400','2026-01-02')""",
                        (self.product_id,self.warehouse_id))
        movement_id = self.db.execute("SELECT id FROM stock_movements WHERE movement_no='R2'").fetchone()["id"]
        self.db.execute("""INSERT INTO stock_valuation_layers(product_id,warehouse_id,source_movement_id,qty_in,qty_remaining,unit_cost)
                           VALUES(?,?,?,'100','100','14')""",(self.product_id,self.warehouse_id,movement_id))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_product_uom_options_reads_custom_code_column(self):
        unit=self.db.execute(
            "SELECT id,category_id FROM stock_uoms WHERE code='PCS'"
        ).fetchone()
        self.db.execute(
            "UPDATE stock_products SET base_uom_id=? WHERE id=?",
            (unit["id"],self.product_id)
        )
        self.db.execute("""INSERT INTO stock_product_uoms(
            product_id,uom_id,custom_code,custom_name,factor_to_base,is_base,
            purchase_enabled,sales_enabled)
            VALUES(?,?, 'PCS-CUSTOM','قطعة مخصصة','1',1,1,1)""",
            (self.product_id,unit["id"]))
        options=stock.product_uom_options(self.db,self.product_id)
        self.assertTrue(any(
            option["code"]=="PCS-CUSTOM" and option["name_ar"]=="قطعة مخصصة"
            for option in options
        ))

    def test_fifo_cost(self):
        cost, allocations = stock.consume_layers(self.db,self.product_id,self.warehouse_id,stock.D("50"),"fifo")
        self.assertEqual(cost, stock.D("500.00"))
        self.assertEqual(allocations[0][2], stock.D("10"))

    def test_moving_average_cost(self):
        cost, allocations = stock.consume_layers(self.db,self.product_id,self.warehouse_id,stock.D("50"),"average")
        self.assertEqual(cost, stock.D("600.00"))

    def test_negative_stock_is_rejected(self):
        with self.assertRaisesRegex(ValueError,"الرصيد غير كافٍ"):
            stock.consume_layers(self.db,self.product_id,self.warehouse_id,stock.D("201"),"fifo")


    def test_interbranch_transfer_posts_destination_debit_and_source_credit(self):
        self.db.execute("""CREATE TABLE accounts(
            id INTEGER PRIMARY KEY,code TEXT,is_group INTEGER,active INTEGER,account_type TEXT)""")
        self.db.execute("INSERT INTO accounts VALUES(1,'1185',0,1,'asset')")
        source_branch=self.db.execute(
            "INSERT INTO branches(code,name_ar) VALUES('BR-A','فرع أ')"
        ).lastrowid
        target_branch=self.db.execute(
            "INSERT INTO branches(code,name_ar) VALUES('BR-B','فرع ب')"
        ).lastrowid
        movement_id=self.db.execute("""INSERT INTO stock_movements(
            movement_no,movement_type,product_id,source_warehouse_id,target_warehouse_id,
            quantity,unit_cost,total_cost,movement_date)
            VALUES('TR-BR-1','transfer',?,?,?,'4','25','100','2026-11-14')""",
            (self.product_id,self.warehouse_id,self.warehouse_id)).lastrowid
        with patch("stock.post_journal",return_value=991) as post:
            result=stock.post_interbranch_transfer_journal(
                self.db,movement_id,"TR-BR-1",{"name_ar":"صنف اختبار"},
                "2026-11-14",Decimal("100"),7,source_branch,target_branch
            )
        self.assertEqual(result,991)
        lines=post.call_args.args[4]
        self.assertEqual(lines[0][1:3],(Decimal("100.00"),0))
        self.assertEqual(lines[0][4],{"branch_id":target_branch})
        self.assertEqual(lines[1][1:3],(0,Decimal("100.00")))
        self.assertEqual(lines[1][4],{"branch_id":source_branch})
        saved=self.db.execute(
            "SELECT journal_entry_id,accounting_status FROM stock_movements WHERE id=?",
            (movement_id,)
        ).fetchone()
        self.assertEqual(saved["journal_entry_id"],991)
        self.assertEqual(saved["accounting_status"],"posted")

    def test_same_branch_transfer_remains_non_posting(self):
        movement_id=self.db.execute("""INSERT INTO stock_movements(
            movement_no,movement_type,product_id,source_warehouse_id,target_warehouse_id,
            quantity,unit_cost,total_cost,movement_date)
            VALUES('TR-SAME','transfer',?,?,?,'1','10','10','2026-11-14')""",
            (self.product_id,self.warehouse_id,self.warehouse_id)).lastrowid
        with patch("stock.post_journal") as post:
            result=stock.post_interbranch_transfer_journal(
                self.db,movement_id,"TR-SAME",{"name_ar":"صنف اختبار"},
                "2026-11-14",Decimal("10"),7,3,3
            )
        self.assertIsNone(result)
        post.assert_not_called()
        self.assertEqual(self.db.execute(
            "SELECT accounting_status FROM stock_movements WHERE id=?",(movement_id,)
        ).fetchone()["accounting_status"],"not_required")

    def test_reorder_alerts_are_scoped_to_each_warehouse(self):
        second_wh=self.db.execute(
            "INSERT INTO stock_warehouses(code,name_ar) VALUES('W-SECOND','مخزن ثاني')"
        ).lastrowid
        second_move=self.db.execute("""INSERT INTO stock_movements(
            movement_no,movement_type,product_id,target_warehouse_id,quantity,
            unit_cost,total_cost,movement_date)
            VALUES('R-SECOND','receipt',? ,?,'100','10','1000','2026-11-14')""",
            (self.product_id,second_wh)).lastrowid
        self.db.execute("""INSERT INTO stock_valuation_layers(
            product_id,warehouse_id,source_movement_id,qty_in,qty_remaining,unit_cost,value_remaining)
            VALUES(?,?,?,'100','100','10','1000')""",
            (self.product_id,second_wh,second_move))
        self.db.execute("""INSERT INTO stock_warehouse_product_settings(
            product_id,warehouse_id,min_qty,reorder_point,reorder_qty)
            VALUES(?,?, '5','250','20'),(?,?, '10','80','30')""",
            (self.product_id,self.warehouse_id,self.product_id,second_wh))
        totals={
            (self.product_id,self.warehouse_id):{"qty":Decimal("200")},
            (self.product_id,second_wh):{"qty":Decimal("100")},
        }
        alerts=stock.calculate_warehouse_reorder_alerts(self.db,totals)
        self.assertEqual(len(alerts),1)
        self.assertEqual(alerts[0]["warehouse_code"],"W1")
        self.assertEqual(alerts[0]["qty"],Decimal("200"))
        self.assertEqual(alerts[0]["point"],Decimal("250"))

    def test_minimum_stock_is_used_when_reorder_point_is_zero(self):
        self.db.execute("""INSERT INTO stock_warehouse_product_settings(
            product_id,warehouse_id,min_qty,reorder_point,reorder_qty)
            VALUES(?,?, '8','0','0')""",(self.product_id,self.warehouse_id))
        alerts=stock.calculate_warehouse_reorder_alerts(
            self.db,{(self.product_id,self.warehouse_id):{"qty":Decimal("4")}}
        )
        self.assertEqual(len(alerts),1)
        self.assertEqual(alerts[0]["point"],Decimal("8"))
        self.assertEqual(alerts[0]["suggested"],Decimal("8"))

    def test_default_product_location_rejects_location_from_another_warehouse(self):
        second_wh=self.db.execute(
            "INSERT INTO stock_warehouses(code,name_ar) VALUES('W2','مخزن آخر')"
        ).lastrowid
        first_loc=self.db.execute("""INSERT INTO stock_locations(
            warehouse_id,code,name_ar,location_type) VALUES(?,'F1','دور 1','floor')""",
            (self.warehouse_id,)).lastrowid
        with self.assertRaisesRegex(Exception,"موقع الصنف الافتراضي"):
            self.db.execute("""INSERT INTO stock_product_locations(
                product_id,warehouse_id,location_id,is_default) VALUES(?,?,?,1)""",
                (self.product_id,second_wh,first_loc))


    def test_issue_without_purpose_is_marked_pending_not_posted(self):
        self.db.execute("""CREATE TABLE company_settings(
            setting_key TEXT PRIMARY KEY,setting_value TEXT)""")
        movement_id=self.db.execute("""INSERT INTO stock_movements(
            movement_no,movement_type,product_id,source_warehouse_id,quantity,
            unit_cost,total_cost,movement_date,accounting_status)
            VALUES('ISSUE-NO-PURPOSE','issue',?,?,'1','10','10','2026-11-14','pending')""",
            (self.product_id,self.warehouse_id)).lastrowid
        result=stock.post_issue_journal(
            self.db,movement_id,"ISSUE-NO-PURPOSE",{"id":self.product_id,"name_ar":"منتج اختبار"},
            "2026-11-14",Decimal("10"),7,None
        )
        self.assertIsNone(result)
        saved=self.db.execute(
            "SELECT journal_entry_id,accounting_status FROM stock_movements WHERE id=?",
            (movement_id,)
        ).fetchone()
        self.assertIsNone(saved["journal_entry_id"])
        self.assertEqual(saved["accounting_status"],"pending_purpose")

    def test_issue_journal_posting_is_idempotent(self):
        movement_id=self.db.execute("""INSERT INTO stock_movements(
            movement_no,movement_type,product_id,quantity,unit_cost,total_cost,
            movement_date,journal_entry_id,accounting_status)
            VALUES('ISSUE-ALREADY-POSTED','issue',?,'1','10','10','2026-11-14',456,'posted')""",
            (self.product_id,)).lastrowid
        with patch("stock.post_journal") as post:
            result=stock.post_issue_journal(
                self.db,movement_id,"ISSUE-ALREADY-POSTED",
                {"id":self.product_id,"name_ar":"منتج اختبار"},
                "2026-11-14",Decimal("10"),7,"sale"
            )
        self.assertEqual(result,456)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
