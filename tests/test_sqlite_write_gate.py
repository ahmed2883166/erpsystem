import threading
import time
import unittest

from sqlite_write_gate import SQLiteWriteGate


class SQLiteWriteGateTests(unittest.TestCase):
    def test_waiters_enter_in_fifo_order(self):
        gate = SQLiteWriteGate()
        entered = []
        first_ready = threading.Event()
        release_first = threading.Event()

        def first():
            with gate.hold():
                first_ready.set()
                release_first.wait(2)
                entered.append("first")

        def queued(label):
            with gate.hold(timeout=2):
                entered.append(label)

        owner = threading.Thread(target=first)
        owner.start()
        self.assertTrue(first_ready.wait(1))
        second = threading.Thread(target=queued, args=("second",))
        third = threading.Thread(target=queued, args=("third",))
        second.start()
        deadline = time.monotonic() + 1
        while gate.waiting_count < 1 and time.monotonic() < deadline:
            time.sleep(0.001)
        third.start()
        deadline = time.monotonic() + 1
        while gate.waiting_count < 2 and time.monotonic() < deadline:
            time.sleep(0.001)
        release_first.set()
        for thread in (owner, second, third):
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(entered, ["first", "second", "third"])

    def test_timeout_removes_waiter_and_next_writer_can_enter(self):
        gate = SQLiteWriteGate()
        entered = threading.Event()
        with gate.hold():
            errors = []

            def timed_waiter():
                try:
                    with gate.hold(timeout=0.02):
                        self.fail("timed-out writer must not enter")
                except TimeoutError:
                    errors.append("timeout")

            thread = threading.Thread(target=timed_waiter)
            thread.start()
            thread.join(1)
            self.assertEqual(errors, ["timeout"])
            self.assertEqual(gate.waiting_count, 0)
        with gate.hold(timeout=0.2):
            entered.set()
        self.assertTrue(entered.is_set())

    def test_gate_releases_after_exception(self):
        gate = SQLiteWriteGate()
        with self.assertRaisesRegex(ValueError, "sentinel"):
            with gate.hold():
                raise ValueError("sentinel")
        with gate.hold(timeout=0.2):
            self.assertEqual(gate.waiting_count, 0)

    def test_wait_duration_is_non_negative(self):
        gate = SQLiteWriteGate()
        with gate.hold() as waited:
            self.assertGreaterEqual(waited, 0)


if __name__ == "__main__":
    unittest.main()
