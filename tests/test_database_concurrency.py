"""Eight-client SQLite write-concurrency regression tests.

These exercise the same connection factory and journal posting function used by
the ERP, against isolated temporary files. They never open the company database.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest

from accounting_core import post_journal
from database import connect_database, enable_wal


def prepare_database(db_path):
    enable_wal(db_path)
    setup = connect_database(db_path)
    try:
        setup.executescript("""
            CREATE TABLE accounts(
                id INTEGER PRIMARY KEY, code TEXT NOT NULL, name_ar TEXT NOT NULL,
                account_type TEXT NOT NULL, parent_id INTEGER,
                is_group INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE journal_entries(
                id INTEGER PRIMARY KEY, entry_date TEXT NOT NULL, reference TEXT,
                description TEXT NOT NULL, status TEXT NOT NULL,
                created_by INTEGER, created_at TEXT NOT NULL
            );
            CREATE TABLE journal_lines(
                id INTEGER PRIMARY KEY, entry_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL, debit TEXT NOT NULL,
                credit TEXT NOT NULL, memo TEXT
            );
            INSERT INTO accounts(id,code,name_ar,account_type) VALUES
                (1,'1110','الصندوق','asset'),
                (2,'4100','الإيرادات','revenue');
        """)
        setup.commit()
    finally:
        setup.close()


class EightClientWriteConcurrencyTests(unittest.TestCase):
    CLIENTS = 8
    POSTS_PER_CLIENT = 25

    def test_eight_clients_can_post_journals_without_lost_or_duplicate_writes(self):
        with tempfile.TemporaryDirectory(prefix="nexus-eight-client-") as folder:
            db_path = Path(folder) / "concurrency.sqlite3"
            prepare_database(db_path)
            start = threading.Barrier(self.CLIENTS)

            def client_work(client_number):
                start.wait(timeout=15)
                posted = []
                connection = connect_database(db_path, timeout=20, isolation_level="")
                try:
                    for sequence in range(self.POSTS_PER_CLIENT):
                        reference = f"LAN-{client_number:02d}-{sequence:04d}"
                        connection.execute("BEGIN IMMEDIATE")
                        try:
                            entry_id = post_journal(
                                connection, "2026-11-16", reference,
                                "اختبار تزامن شبكة محلية",
                                [
                                    (1, "125.50", "0", "مدين الصندوق"),
                                    (2, "0", "125.50", "دائن الإيراد"),
                                ],
                                created_by=client_number,
                            )
                            connection.commit()
                            posted.append(entry_id)
                        except Exception:
                            connection.rollback()
                            raise
                    return posted
                finally:
                    connection.close()

            with ThreadPoolExecutor(max_workers=self.CLIENTS) as pool:
                futures = [pool.submit(client_work, number) for number in range(1, self.CLIENTS + 1)]
                results = [future.result(timeout=90) for future in futures]

            expected_entries = self.CLIENTS * self.POSTS_PER_CLIENT
            self.assertEqual(sum(len(items) for items in results), expected_entries)
            self.assertEqual(len({entry_id for items in results for entry_id in items}), expected_entries)

            verify = connect_database(db_path)
            try:
                self.assertEqual(verify.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(verify.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0], expected_entries)
                self.assertEqual(verify.execute("SELECT COUNT(*) FROM journal_lines").fetchone()[0], expected_entries * 2)
                self.assertEqual(verify.execute("""
                    SELECT COUNT(*) FROM (
                        SELECT reference FROM journal_entries
                        GROUP BY reference HAVING COUNT(*) <> 1
                    )
                """).fetchone()[0], 0)
                self.assertEqual(verify.execute("""
                    SELECT COUNT(*) FROM (
                        SELECT entry_id, DECIMAL_SUM(debit) AS debit_total,
                            DECIMAL_SUM(credit) AS credit_total
                        FROM journal_lines GROUP BY entry_id
                        HAVING debit_total <> credit_total
                    )
                """).fetchone()[0], 0)
            finally:
                verify.close()

    def test_eight_clients_posting_the_same_reference_create_only_one_journal(self):
        with tempfile.TemporaryDirectory(prefix="nexus-eight-client-idempotency-") as folder:
            db_path = Path(folder) / "idempotency.sqlite3"
            prepare_database(db_path)
            start = threading.Barrier(self.CLIENTS)

            def client_work(client_number):
                start.wait(timeout=15)
                connection = connect_database(db_path, timeout=20, isolation_level="")
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        entry_id = post_journal(
                            connection, "2026-11-16", "LAN-SAME-REFERENCE",
                            "اختبار منع تكرار القيد",
                            [
                                (1, "250.00", "0", "مدين الصندوق"),
                                (2, "0", "250.00", "دائن الإيراد"),
                            ],
                            created_by=client_number,
                        )
                        connection.commit()
                        return entry_id
                    except Exception:
                        connection.rollback()
                        raise
                finally:
                    connection.close()

            with ThreadPoolExecutor(max_workers=self.CLIENTS) as pool:
                futures = [pool.submit(client_work, number) for number in range(1, self.CLIENTS + 1)]
                entry_ids = [future.result(timeout=90) for future in futures]

            self.assertEqual(len(set(entry_ids)), 1)
            verify = connect_database(db_path)
            try:
                self.assertEqual(verify.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0], 1)
                self.assertEqual(verify.execute("SELECT COUNT(*) FROM journal_lines").fetchone()[0], 2)
            finally:
                verify.close()


if __name__ == "__main__":
    unittest.main()
