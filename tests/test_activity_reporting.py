import sqlite3
import unittest
from decimal import Decimal

from activity_reporting import build_activity_performance_report


class ActivityReportingTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE company_settings(setting_key TEXT PRIMARY KEY, setting_value TEXT)")
        self.conn.execute("INSERT INTO company_settings VALUES('activity','commercial')")
        self.conn.executescript("""
            CREATE TABLE sales_invoices(
              id INTEGER PRIMARY KEY, activity_code TEXT, subtotal TEXT, tax_amount TEXT,
              total_amount TEXT, status TEXT, journal_entry_id INTEGER, cogs_journal_entry_id INTEGER
            );
            CREATE TABLE supplier_bills(
              id INTEGER PRIMARY KEY, activity_code TEXT, subtotal TEXT, tax_amount TEXT,
              total_amount TEXT, status TEXT, journal_entry_id INTEGER
            );
            CREATE TABLE goods_receipts(
              id INTEGER PRIMARY KEY, activity_code TEXT, total_value TEXT,
              status TEXT, journal_entry_id INTEGER
            );
            CREATE TABLE stock_movements(
              id INTEGER PRIMARY KEY, activity_code TEXT, movement_type TEXT,
              total_cost TEXT, accounting_status TEXT, journal_entry_id INTEGER
            );
            CREATE TABLE journal_lines(
              id INTEGER PRIMARY KEY, entry_id INTEGER, debit TEXT, credit TEXT
            );
        """)

    def tearDown(self):
        self.conn.close()

    def test_groups_documents_and_deduplicates_shared_journal_links(self):
        self.conn.execute(
            "INSERT INTO sales_invoices VALUES(1,'commercial','100','14','114','posted',1,2)"
        )
        self.conn.execute(
            "INSERT INTO supplier_bills VALUES(1,'service','50','7','57','posted',3)"
        )
        self.conn.execute(
            "INSERT INTO goods_receipts VALUES(1,'commercial','80','posted',4)"
        )
        self.conn.executemany(
            "INSERT INTO stock_movements VALUES(?,?,?,?,?,?)",
            [
                (1,'commercial','receipt','80','posted',4),
                (2,'commercial','issue','25','posted',2),
            ],
        )
        self.conn.executemany(
            "INSERT INTO journal_lines(entry_id,debit,credit) VALUES(?,?,?)",
            [
                (1,'114','114'), (2,'25','25'), (3,'57','57'), (4,'80','80'),
            ],
        )
        result = build_activity_performance_report(self.conn)
        self.assertEqual(result["journal_entry_count"], 4)
        self.assertEqual(result["activity_count"], 2)
        commercial = next(x for x in result["activities"] if x["activity_code"] == "commercial")
        service = next(x for x in result["activities"] if x["activity_code"] == "service")
        self.assertEqual(commercial["sales_total"], Decimal("114.00"))
        self.assertEqual(commercial["stock_receipt_value"], Decimal("80.00"))
        self.assertEqual(commercial["stock_issue_value"], Decimal("25.00"))
        self.assertEqual(service["purchase_total"], Decimal("57.00"))
        self.assertEqual(result["unbalanced_journal_count"], 0)

    def test_legacy_documents_fall_back_to_company_activity(self):
        self.conn.execute(
            "INSERT INTO sales_invoices VALUES(1,NULL,'10','0','10','posted',1,NULL)"
        )
        self.conn.execute(
            "INSERT INTO journal_lines(entry_id,debit,credit) VALUES(1,'10','10')"
        )
        result = build_activity_performance_report(self.conn)
        self.assertEqual(result["activities"][0]["activity_code"], "commercial")
        self.assertEqual(result["activities"][0]["sales_total"], Decimal("10.00"))

    def test_one_journal_entry_cannot_be_attributed_to_two_activities(self):
        self.conn.execute(
            "INSERT INTO sales_invoices VALUES(1,'commercial','10','0','10','posted',1,NULL)"
        )
        self.conn.execute(
            "INSERT INTO supplier_bills VALUES(1,'service','10','0','10','posted',1)"
        )
        with self.assertRaisesRegex(ValueError, "أكثر من نشاط"):
            build_activity_performance_report(self.conn)


if __name__ == "__main__":
    unittest.main()
