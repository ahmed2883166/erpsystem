import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
