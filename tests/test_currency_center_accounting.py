"""Accounting safety baseline for the foreign-currency subledger."""
import os
import sqlite3
import unittest
from decimal import Decimal

import currency_center


class CurrencyDatabaseErrorDetectionTests(unittest.TestCase):
    def test_sqlite_errors_are_recognized(self):
        self.assertTrue(currency_center._is_database_error(sqlite3.OperationalError("locked")))

    def test_psycopg_errors_are_recognized_without_importing_driver(self):
        class DriverError(Exception):
            pass
        DriverError.__module__ = "psycopg.errors"
        self.assertTrue(currency_center._is_database_error(DriverError("database failure")))

    def test_unexpected_programming_errors_are_not_swallowed(self):
        self.assertFalse(currency_center._is_database_error(RuntimeError("bug")))


class CurrencyAmountTests(unittest.TestCase):
    def test_decimal_parser_rejects_invalid_and_non_finite_values(self):
        for value in ("not-a-number", "NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    currency_center.D(value)

    def test_money_rounds_half_up_to_two_decimal_places(self):
        self.assertEqual(currency_center.M("1.005"), Decimal("1.01"))
        self.assertEqual(currency_center.M("1.004"), Decimal("1.00"))
        self.assertEqual(currency_center.M("-1.005"), Decimal("-1.01"))


class CurrencyWriteLockTests(unittest.TestCase):
    def test_sqlite_write_lock_starts_immediate_transaction(self):
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("""
                CREATE TABLE fx_transactions (
                    id INTEGER PRIMARY KEY,
                    balance_foreign TEXT NOT NULL,
                    status TEXT NOT NULL
                )
            """)
            conn.execute(
                "INSERT INTO fx_transactions(id,balance_foreign,status) VALUES(1,'100.00','open')"
            )
            currency_center._begin_fx_write(conn)
            self.assertTrue(conn.in_transaction)
            row = currency_center._open_fx_transaction_for_update(conn, 1)
            self.assertEqual(row["balance_foreign"], "100.00")
            conn.rollback()
        finally:
            conn.close()


class CurrencyBalanceUpdateTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""
            CREATE TABLE fx_transactions(
                id INTEGER PRIMARY KEY,
                balance_foreign TEXT NOT NULL,
                balance_base TEXT NOT NULL,
                status TEXT NOT NULL
            )
        """)
        self.conn.execute(
            "INSERT INTO fx_transactions(id,balance_foreign,balance_base,status) "
            "VALUES(1,'100.00','500.00','open')"
        )

    def tearDown(self):
        self.conn.close()

    def test_partial_settlement_keeps_transaction_open(self):
        currency_center.update_open_fx_balance(
            self.conn, 1, "100.00", Decimal("60.00"), Decimal("300.00")
        )
        row = self.conn.execute(
            "SELECT balance_foreign,balance_base,status FROM fx_transactions WHERE id=1"
        ).fetchone()
        self.assertEqual(row["balance_foreign"], "60.00")
        self.assertEqual(row["balance_base"], "300.00")
        self.assertEqual(row["status"], "open")

    def test_full_settlement_marks_transaction_settled(self):
        currency_center.update_open_fx_balance(
            self.conn, 1, "100.00", Decimal("0.00"), Decimal("0.00")
        )
        row = self.conn.execute(
            "SELECT balance_foreign,balance_base,status FROM fx_transactions WHERE id=1"
        ).fetchone()
        self.assertEqual(row["balance_foreign"], "0")
        self.assertEqual(row["balance_base"], "0.00")
        self.assertEqual(row["status"], "settled")

    def test_stale_expected_balance_is_rejected_without_overwriting(self):
        with self.assertRaisesRegex(ValueError, "تغير رصيد المعاملة"):
            currency_center.update_open_fx_balance(
                self.conn, 1, "90.00", Decimal("50.00"), Decimal("250.00")
            )
        row = self.conn.execute(
            "SELECT balance_foreign,balance_base,status FROM fx_transactions WHERE id=1"
        ).fetchone()
        self.assertEqual(row["balance_foreign"], "100.00")
        self.assertEqual(row["balance_base"], "500.00")
        self.assertEqual(row["status"], "open")


@unittest.skipUnless(
    os.environ.get("NEXUS_TEST_POSTGRES_DSN"),
    "PostgreSQL integration DSN not configured",
)
class CurrencyBalanceUpdatePostgreSQLTests(unittest.TestCase):
    def test_fx_write_lock_and_row_lock_work_in_disposable_schema(self):
        import uuid
        import psycopg
        from psycopg.rows import dict_row

        schema = "nexus_fx_lock_" + uuid.uuid4().hex[:12]
        conn = psycopg.connect(
            os.environ["NEXUS_TEST_POSTGRES_DSN"], row_factory=dict_row
        )
        try:
            with conn.transaction():
                conn.execute(f'CREATE SCHEMA "{schema}"')
                conn.execute(f'SET LOCAL search_path TO "{schema}"')
                conn.execute("""
                    CREATE TABLE fx_transactions (
                        id BIGINT PRIMARY KEY,
                        balance_foreign TEXT NOT NULL,
                        status TEXT NOT NULL
                    )
                """)
                conn.execute(
                    "INSERT INTO fx_transactions(id,balance_foreign,status) VALUES(1,'100.00','open')"
                )
                currency_center._begin_fx_write(conn)
                row = currency_center._open_fx_transaction_for_update(conn, 1)
                self.assertEqual(row["balance_foreign"], "100.00")
                raise RuntimeError("rollback disposable schema")
        except RuntimeError as exc:
            self.assertEqual(str(exc), "rollback disposable schema")
        finally:
            conn.close()



    def test_full_settlement_updates_row_in_rolled_back_schema(self):
        import uuid
        import psycopg
        from psycopg.rows import dict_row

        schema = "nexus_fx_balance_" + uuid.uuid4().hex[:12]
        conn = psycopg.connect(
            os.environ["NEXUS_TEST_POSTGRES_DSN"], row_factory=dict_row
        )
        try:
            try:
                with conn.transaction():
                    conn.execute(f'CREATE SCHEMA "{schema}"')
                    conn.execute(f'SET LOCAL search_path TO "{schema}"')
                    conn.execute("""
                        CREATE TABLE fx_transactions (
                            id BIGINT PRIMARY KEY,
                            balance_foreign TEXT NOT NULL,
                            balance_base TEXT NOT NULL,
                            status TEXT NOT NULL
                        )
                    """)
                    conn.execute("""
                        INSERT INTO fx_transactions
                            (id,balance_foreign,balance_base,status)
                        VALUES (1,'100.00','500.00','open')
                    """)
                    currency_center.update_open_fx_balance(
                        conn, 1, "100.00", Decimal("0.00"), Decimal("0.00")
                    )
                    row = conn.execute("""
                        SELECT balance_foreign,balance_base,status
                        FROM fx_transactions WHERE id=1
                    """).fetchone()
                    self.assertEqual(
                        (row["balance_foreign"], row["balance_base"], row["status"]),
                        ("0", "0.00", "settled"),
                    )
                    raise RuntimeError("rollback disposable schema")
            except RuntimeError as exc:
                self.assertEqual(str(exc), "rollback disposable schema")
        finally:
            conn.close()


    def test_row_lock_serializes_two_connections_and_prevents_stale_balance(self):
        """A waiter must read the balance committed by the transaction holding FOR UPDATE."""
        import threading
        import time
        import uuid
        import psycopg
        from psycopg.rows import dict_row

        schema = "nexus_fx_race_" + uuid.uuid4().hex[:12]
        dsn = os.environ["NEXUS_TEST_POSTGRES_DSN"]
        first = psycopg.connect(dsn, row_factory=dict_row)
        second = psycopg.connect(dsn, row_factory=dict_row)
        monitor = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
        started = threading.Event()
        acquired = threading.Event()
        observed = {}
        errors = []
        try:
            first.execute(f'CREATE SCHEMA "{schema}"')
            first.commit()
            first.execute(f'SET search_path TO "{schema}"')
            second.execute(f'SET search_path TO "{schema}"')
            first.execute("""
                CREATE TABLE fx_transactions (
                    id BIGINT PRIMARY KEY,
                    balance_foreign TEXT NOT NULL,
                    status TEXT NOT NULL
                )
            """)
            first.execute(
                "INSERT INTO fx_transactions(id,balance_foreign,status) VALUES(1,'100.00','open')"
            )
            first.commit()

            currency_center._begin_fx_write(first)
            row = currency_center._open_fx_transaction_for_update(first, 1)
            self.assertEqual(row["balance_foreign"], "100.00")

            def waiter():
                try:
                    _ = currency_center._begin_fx_write(second)
                    started.set()
                    locked_row = currency_center._open_fx_transaction_for_update(second, 1)
                    observed["balance"] = locked_row["balance_foreign"]
                    acquired.set()
                    second.commit()
                except Exception as exc:  # surfaced in the main test thread
                    errors.append(exc)
                    acquired.set()

            worker = threading.Thread(target=waiter, daemon=True)
            worker.start()
            self.assertTrue(started.wait(2), "second connection did not start")
            second_pid = second.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
            deadline = time.monotonic() + 5
            waiting_on_lock = False
            while time.monotonic() < deadline:
                activity = monitor.execute(
                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s",
                    (second_pid,),
                ).fetchone()
                if activity and activity["wait_event_type"] == "Lock":
                    waiting_on_lock = True
                    break
                if acquired.is_set():
                    break
                time.sleep(0.02)
            self.assertTrue(
                waiting_on_lock,
                "second connection never entered a PostgreSQL lock wait while first held the row",
            )
            self.assertFalse(acquired.is_set())

            first.execute(
                "UPDATE fx_transactions SET balance_foreign='60.00' WHERE id=1"
            )
            first.commit()
            self.assertTrue(acquired.wait(5), "second connection stayed blocked after commit")
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive(), "row-lock waiter did not finish")
            self.assertFalse(errors, f"row-lock waiter failed: {errors!r}")
            self.assertEqual(observed.get("balance"), "60.00")
        finally:
            try:
                first.rollback()
                second.rollback()
                first.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                first.commit()
            finally:
                first.close()
                second.close()
                monitor.close()


if __name__ == "__main__":
    unittest.main()