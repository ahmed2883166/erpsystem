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
                gate = first._nexus_write_gate
                first.execute("BEGIN IMMEDIATE")

                waiter_started = threading.Event()
                waiter_acquired = threading.Event()
                failures = []
                second_gate = []

                def waiting_writer():
                    second = None
                    try:
                        # SQLite connections are thread-affine by default:
                        # create and close this connection in its owning thread.
                        second = connect_database(path, isolation_level=None)
                        second_gate.append(second._nexus_write_gate)
                        waiter_started.set()
                        second.execute("BEGIN IMMEDIATE")
                        waiter_acquired.set()
                        second.execute("ROLLBACK")
                    except Exception as exc:
                        failures.append(exc)
                    finally:
                        if second is not None:
                            second.close()

                thread = threading.Thread(target=waiting_writer)
                try:
                    thread.start()
                    self.assertTrue(waiter_started.wait(1))
                    self.assertIs(second_gate[0], gate)
                    deadline = time.monotonic() + 2
                    while gate.waiting_count < 1 and time.monotonic() < deadline:
                        time.sleep(0.001)
                    self.assertEqual(gate.waiting_count, 1)
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


if __name__ == "__main__":
    unittest.main()
