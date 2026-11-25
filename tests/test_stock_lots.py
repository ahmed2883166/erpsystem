import sqlite3
import unittest

from stock import make_lot


class StockLotExpiryTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""CREATE TABLE stock_lots(
            id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL, lot_number TEXT NOT NULL,
            production_date TEXT, expiry_date TEXT, UNIQUE(product_id,lot_number)
        )""")
        self.product = {"id": 7, "track_lot": 1, "track_expiry": 1}

    def tearDown(self):
        self.conn.close()

    def test_receipt_saves_lot_production_and_expiry_dates(self):
        lot_id = make_lot(self.conn, self.product, "LOT-001",
                          "2026-01-01", "2027-01-01", "2026-11-11")
        row = self.conn.execute("SELECT * FROM stock_lots WHERE id=?", (lot_id,)).fetchone()
        self.assertEqual(row["production_date"], "2026-01-01")
        self.assertEqual(row["expiry_date"], "2027-01-01")

    def test_tracked_expiry_requires_expiry_date(self):
        with self.assertRaisesRegex(ValueError, "تاريخ الصلاحية مطلوب"):
            make_lot(self.conn, self.product, "LOT-002", "", "", "2026-11-11")

    def test_receipt_rejects_expired_lot(self):
        with self.assertRaisesRegex(ValueError, "منتهية الصلاحية"):
            make_lot(self.conn, self.product, "LOT-003",
                     "2025-01-01", "2026-01-01", "2026-11-11")

    def test_receipt_rejects_production_date_after_receipt(self):
        with self.assertRaisesRegex(ValueError, "بعد تاريخ الاستلام"):
            make_lot(self.conn, self.product, "LOT-004",
                     "2026-12-01", "2027-01-01", "2026-11-11")

    def test_existing_lot_cannot_change_expiry_date(self):
        make_lot(self.conn, self.product, "LOT-005",
                 "2026-01-01", "2027-01-01", "2026-11-11")
        with self.assertRaisesRegex(ValueError, "بتواريخ إنتاج أو صلاحية مختلفة"):
            make_lot(self.conn, self.product, "LOT-005",
                     "2026-01-01", "2027-02-01", "2026-11-11")


if __name__ == "__main__":
    unittest.main()
