import sqlite3
import unittest

from stock import lock_stock_warehouses


class StockWarehouseLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            "CREATE TABLE stock_warehouses(id INTEGER PRIMARY KEY, active INTEGER NOT NULL)"
        )
        self.connection.executemany(
            "INSERT INTO stock_warehouses(id, active) VALUES(?, ?)",
            [(1, 1), (2, 0)],
        )

    def tearDown(self):
        self.connection.close()

    def test_new_mutations_reject_inactive_warehouse(self):
        with self.assertRaisesRegex(ValueError, "حركة جديدة"):
            lock_stock_warehouses(self.connection, 2)

    def test_historical_reversal_can_reference_inactive_warehouse(self):
        rows = lock_stock_warehouses(self.connection, 2, allow_inactive=True)
        self.assertEqual([row["id"] for row in rows], [2])

    def test_missing_warehouse_is_rejected_even_for_historical_reversal(self):
        with self.assertRaisesRegex(ValueError, "غير موجود"):
            lock_stock_warehouses(self.connection, 99, allow_inactive=True)

    def test_active_warehouse_remains_available_for_new_mutations(self):
        rows = lock_stock_warehouses(self.connection, 1)
        self.assertEqual([row["id"] for row in rows], [1])


if __name__ == "__main__":
    unittest.main()
