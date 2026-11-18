"""Integration checks for the process-local SQLite writer admission gate."""
from pathlib import Path
import tempfile
import threading
import time
import unittest

from database import connect_database


class SQLiteConnectionWriteGateIntegrationTests(unittest.TestCase):
    def test_connections_share_gate_and_sql_transaction_end_releases_waiter(self):
        for end_statement in ("COMMIT", "ROLLBACK"):
            with self.subTest(end_statement=end_statement), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "shared-gate.sqlite3"
                first = connect_database(path, isolation_level=None)
                second = connect_database(path, isolation_level=None)
                self.assertIs(first._nexus_write_gate, second._nexus_write_gate)
                first.execute("BEGIN IMMEDIATE")

                waiter_started = threading.Event()
                waiter_acquired = threading.Event()
                failures = []

                def waiting_writer():
                    waiter_started.set()
                    try:
                        second.execute("BEGIN IMMEDIATE")
                        waiter_acquired.set()
                        second.execute("ROLLBACK")
                    except Exception as exc:
                        failures.append(exc)

                thread = threading.Thread(target=waiting_writer)
                try:
                    thread.start()
                    self.assertTrue(waiter_started.wait(1))
                    deadline = time.monotonic() + 2
                    while first._nexus_write_gate.waiting_count < 1 and time.monotonic() < deadline:
                        time.sleep(0.001)
                    self.assertEqual(first._nexus_write_gate.waiting_count, 1)
                    self.assertFalse(waiter_acquired.wait(0.05))

                    first.execute(end_statement)
                    self.assertTrue(waiter_acquired.wait(2))
                    thread.join(2)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(failures, [])
                finally:
                    if thread.is_alive():
                        first.rollback()
                        thread.join(2)
                    first.close()
                    second.close()


if __name__ == "__main__":
    unittest.main()
