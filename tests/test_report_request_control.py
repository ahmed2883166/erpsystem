import sqlite3
import threading
import time
import unittest
import tempfile
from pathlib import Path

from database import connect_database

from report_request_control import (
    ReportRequestGate,
    bounded_env_int,
    is_expensive_report_request,
    make_sqlite_deadline_callback,
    set_current_report_deadline,
    reset_current_report_deadline,
)


class ReportRequestControlTests(unittest.TestCase):
    def test_only_read_only_report_requests_are_limited(self):
        # The home page endpoint is named "dashboard" but must never be throttled.
        self.assertFalse(is_expensive_report_request(
            "GET", "dashboard", "/"
        ))
        self.assertFalse(is_expensive_report_request(
            "GET", "dashboard", "/dashboard"
        ))
        self.assertFalse(is_expensive_report_request(
            "GET", "dashboard_settings", "/dashboard/settings"
        ))
        self.assertTrue(is_expensive_report_request(
            "GET", "management_dashboard", "/dashboard/management"
        ))
        self.assertTrue(is_expensive_report_request(
            "GET", "management_dashboard", "/dashboard/management/"
        ))
        self.assertTrue(is_expensive_report_request(
            "GET", "income_statement", "/reports/income-statement"
        ))
        self.assertTrue(is_expensive_report_request(
            "GET", "payroll_report", "/hr/payroll-report"
        ))
        self.assertFalse(is_expensive_report_request(
            "POST", "create_journal", "/journal/new"
        ))
        self.assertFalse(is_expensive_report_request(
            "GET", "sales_invoice_new", "/sales/invoices/new"
        ))

    def test_report_gate_wait_queue_is_bounded_and_releases(self):
        gate = ReportRequestGate(1, max_waiters=1, wait_seconds=2)
        self.assertTrue(gate.acquire())
        acquired = []
        waiter = threading.Thread(target=lambda: acquired.append(gate.acquire()))
        waiter.start()

        wait_deadline = time.monotonic() + 1
        while gate.waiting_count != 1 and time.monotonic() < wait_deadline:
            time.sleep(0.001)
        self.assertEqual(gate.waiting_count, 1)
        # A second waiter is rejected immediately when the queue is full.
        self.assertFalse(gate.acquire())

        gate.release()
        waiter.join(timeout=2)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(acquired, [True])
        gate.release()

    def test_report_gate_waiter_times_out_and_does_not_leak_queue_capacity(self):
        gate = ReportRequestGate(1, max_waiters=1, wait_seconds=1)
        self.assertTrue(gate.acquire())
        acquired = []
        waiter = threading.Thread(target=lambda: acquired.append(gate.acquire()))
        waiter.start()
        wait_deadline = time.monotonic() + 1
        while gate.waiting_count != 1 and time.monotonic() < wait_deadline:
            time.sleep(0.001)
        self.assertEqual(gate.waiting_count, 1)

        waiter.join(timeout=2)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(acquired, [False])
        self.assertEqual(gate.waiting_count, 0)
        gate.release()
        self.assertTrue(gate.acquire())
        gate.release()

    def test_invalid_and_out_of_range_environment_values_are_bounded(self):
        self.assertEqual(bounded_env_int("not-a-number", 90, 5, 180), 90)
        self.assertEqual(bounded_env_int("1", 90, 5, 180), 5)
        self.assertEqual(bounded_env_int("999", 90, 5, 180), 180)

    def test_sqlite_progress_handler_interrupts_work_after_deadline(self):
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE TABLE numbers(value INTEGER)")
            connection.executemany(
                "INSERT INTO numbers(value) VALUES(?)",
                ((number,) for number in range(10000)),
            )
            connection.set_progress_handler(
                make_sqlite_deadline_callback(time.monotonic() - 1), 1
            )
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute(
                    "SELECT sum(a.value * b.value) "
                    "FROM numbers a CROSS JOIN numbers b"
                ).fetchone()
        finally:
            connection.close()


    def test_shared_database_factory_applies_deadline_to_blueprint_connections(self):
        with tempfile.TemporaryDirectory(prefix="nexus-report-deadline-") as folder:
            db_path = Path(folder) / "report.sqlite3"
            token = set_current_report_deadline(time.monotonic() - 1)
            connection = None
            try:
                connection = connect_database(db_path)
                connection.execute("CREATE TABLE numbers(value INTEGER)")
                connection.executemany(
                    "INSERT INTO numbers(value) VALUES(?)",
                    ((number,) for number in range(1000)),
                )
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute(
                        "SELECT sum(a.value * b.value) "
                        "FROM numbers a CROSS JOIN numbers b"
                    ).fetchone()
            finally:
                if connection is not None:
                    connection.close()
                reset_current_report_deadline(token)


if __name__ == "__main__":
    unittest.main()
