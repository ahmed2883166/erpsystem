import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("NEXUS_SECRET_KEY", "unit-test-secret-key-which-is-long-enough-123")
os.environ["NEXUS_SKIP_DB_INIT"] = "1"

import app as nexus
import stock


class StocktakeRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_stock_path = stock.DB_PATH
        stock.DB_PATH = Path(self.temp.name) / "stocktake.db"
        stock.initialize_stock_schema()
        conn = stock.get_db()
        conn.execute("INSERT INTO stock_warehouses(code,name_ar) VALUES('W1','المخزن الرئيسي')")
        self.warehouse_id = conn.execute("SELECT id FROM stock_warehouses").fetchone()["id"]
        conn.execute("INSERT INTO stock_products(sku,name_ar) VALUES('P1','صنف تجريبي')")
        self.product_id = conn.execute("SELECT id FROM stock_products").fetchone()["id"]
        conn.execute("""INSERT INTO stock_movements(movement_no,movement_type,product_id,target_warehouse_id,
            quantity,unit_cost,total_cost,movement_date) VALUES('OPEN-1','receipt',?,?, '10','5','50','2026-01-01')""",
            (self.product_id,self.warehouse_id))
        movement_id = conn.execute("SELECT id FROM stock_movements WHERE movement_no='OPEN-1'").fetchone()["id"]
        conn.execute("""INSERT INTO stock_valuation_layers(product_id,warehouse_id,source_movement_id,
            qty_in,qty_remaining,unit_cost,value_remaining) VALUES(?,?,?,'10','10','5','50')""",
            (self.product_id,self.warehouse_id,movement_id))
        conn.execute("CREATE TABLE accounts(id INTEGER PRIMARY KEY,code TEXT,name_ar TEXT,account_type TEXT,is_group INTEGER,active INTEGER)")
        conn.commit()
        conn.close()
        nexus.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.client = nexus.app.test_client()

    def tearDown(self):
        stock.DB_PATH = self.old_stock_path
        self.temp.cleanup()

    def _login(self):
        with self.client.session_transaction() as session:
            session.update(user_id=7, username="stockadmin", full_name="مدير المخزن",
                           role="admin", permissions=["stock.manage"])

    def test_stocktake_captures_book_snapshot_and_saves_actual_variance(self):
        self._login()
        response = self.client.post("/stock/counts", data={
            "warehouse_id": str(self.warehouse_id), "count_date": "2026-11-11",
            "notes": "جرد اختباري",
        })
        self.assertEqual(response.status_code, 302)
        conn = stock.get_db()
        header = conn.execute("SELECT * FROM stocktakes").fetchone()
        line = conn.execute("SELECT * FROM stocktake_lines").fetchone()
        self.assertEqual(header["status"], "draft")
        self.assertEqual(line["snapshot_qty"], "10.0000")
        detail_response = self.client.get("/stock/counts/" + str(header["id"]))
        self.assertEqual(detail_response.status_code, 200)
        response = self.client.post("/stock/counts/" + str(header["id"]), data={
            "counted_" + str(line["id"]): "8", "cost_" + str(line["id"]): "5",
        })
        self.assertEqual(response.status_code, 302)
        updated = conn.execute("SELECT * FROM stocktake_lines WHERE id=?", (line["id"],)).fetchone()
        status = conn.execute("SELECT status FROM stocktakes WHERE id=?", (header["id"],)).fetchone()["status"]
        self.assertEqual(updated["counted_qty"], "8.0000")
        self.assertEqual(updated["variance_qty"], "-2.0000")
        self.assertEqual(status, "counted")
        conn.close()


    def test_approval_is_blocked_if_stock_moved_after_count_started(self):
        self._login()
        self.client.post("/stock/counts", data={
            "warehouse_id": str(self.warehouse_id), "count_date": "2026-11-11",
        })
        conn = stock.get_db()
        header = conn.execute("SELECT * FROM stocktakes").fetchone()
        line = conn.execute("SELECT * FROM stocktake_lines").fetchone()
        self.client.post("/stock/counts/" + str(header["id"]), data={
            "counted_" + str(line["id"]): "10", "cost_" + str(line["id"]): "5",
        })
        conn.execute("""INSERT INTO stock_movements(movement_no,movement_type,product_id,
            target_warehouse_id,quantity,unit_cost,total_cost,movement_date)
            VALUES('AFTER-COUNT','receipt',?,?, '1','5','5','2026-11-11')""",
            (self.product_id,self.warehouse_id))
        conn.commit()
        response = self.client.post("/stock/counts/" + str(header["id"]) + "/approve")
        self.assertEqual(response.status_code, 302)
        status = conn.execute("SELECT status FROM stocktakes WHERE id=?", (header["id"],)).fetchone()["status"]
        self.assertEqual(status, "counted")
        conn.close()

    def test_stocktake_screen_requires_stock_management_permission(self):
        with self.client.session_transaction() as session:
            session.update(user_id=7, username="viewer", role="viewer", permissions=["stock.view"])
        response = self.client.get("/stock/counts")
        self.assertEqual(response.status_code, 302)


if __name__ == "__main__":
    unittest.main()
