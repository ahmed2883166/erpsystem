import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.audit_interbranch_dimensions import audit_interbranch_dimensions


class HistoricalInterbranchDimensionAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "audit.sqlite"
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE branches(id INTEGER PRIMARY KEY, code TEXT, name_ar TEXT);
            CREATE TABLE stock_warehouses(
                id INTEGER PRIMARY KEY, code TEXT, name_ar TEXT, branch_id INTEGER
            );
            CREATE TABLE stock_movements(
                id INTEGER PRIMARY KEY, movement_no TEXT, movement_type TEXT,
                movement_date TEXT, source_warehouse_id INTEGER, target_warehouse_id INTEGER,
                journal_entry_id INTEGER, accounting_status TEXT
            );
            CREATE TABLE journal_entries(id INTEGER PRIMARY KEY, status TEXT);
            CREATE TABLE accounts(id INTEGER PRIMARY KEY, code TEXT);
            CREATE TABLE journal_lines(
                id INTEGER PRIMARY KEY, entry_id INTEGER, account_id INTEGER,
                debit TEXT, credit TEXT, branch_id INTEGER,
                stock_product_id INTEGER, stock_warehouse_id INTEGER
            );
            INSERT INTO branches VALUES(1,'B1','فرع 1');
            INSERT INTO branches VALUES(2,'B2','فرع 2');
            INSERT INTO stock_warehouses VALUES(10,'W1','مخزن 1',1);
            INSERT INTO stock_warehouses VALUES(20,'W2','مخزن 2',2);
            INSERT INTO accounts VALUES(1,'1200');
            INSERT INTO accounts VALUES(2,'1185');
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_detects_missing_branch_dimension_without_modifying_database(self):
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            INSERT INTO journal_entries VALUES(100,'posted');
            INSERT INTO stock_movements VALUES(
                5,'TR-5','transfer','2025-01-01',10,20,100,'posted'
            );
            INSERT INTO journal_lines VALUES(1,100,1,'50','0',1,7,20);
            INSERT INTO journal_lines VALUES(2,100,1,'0','50',NULL,7,10);
            INSERT INTO journal_lines VALUES(3,100,2,'50','0',1,NULL,NULL);
            INSERT INTO journal_lines VALUES(4,100,2,'0','50',2,NULL,NULL);
        """)
        conn.commit()
        conn.close()

        result = audit_interbranch_dimensions(self.db_path)
        self.assertTrue(result["read_only"])
        self.assertEqual(result["movement_count"], 1)
        self.assertIn("inventory_line_missing_branch_dimension",
                      result["movements"][0]["issues"])
        self.assertTrue(result["movements"][0]["requires_manual_review"])

        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    def test_flags_missing_journal_link(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT INTO stock_movements VALUES(
                6,'TR-6','transfer','2025-01-02',10,20,NULL,'pending_setup'
            )
        """)
        conn.commit()
        conn.close()

        result = audit_interbranch_dimensions(self.db_path)
        self.assertIn("missing_journal_link", result["movements"][0]["issues"])
        self.assertFalse(result["passed"])


if __name__ == "__main__":
    unittest.main()
