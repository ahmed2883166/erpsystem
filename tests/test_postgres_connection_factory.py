"""Tests for the guarded PostgreSQL branch in the shared connection factory."""
import time
import unittest
from unittest.mock import patch

from database import connect_database


class FakeRawConnection:
    def __init__(self):
        self.autocommit = False
        self.calls = []

    def execute(self, query, params=None):
        self.calls.append((query, params))
        return self

    def rollback(self):
        self.calls.append(("ROLLBACK", None))


class FakeLease:
    def __init__(self, raw):
        self.raw = raw
        self.exit_calls = []

    def __enter__(self):
        return self.raw

    def __exit__(self, exc_type, exc_value, traceback):
        self.exit_calls.append((exc_type, exc_value, traceback))
        return False


class FakePool:
    def __init__(self, lease):
        self.lease = lease
        self.calls = []

    def connection(self, **kwargs):
        self.calls.append(kwargs)
        return self.lease


class PostgresConnectionFactoryTests(unittest.TestCase):
    def test_postgresql_branch_uses_process_pool_and_report_deadline(self):
        raw = FakeRawConnection()
        lease = FakeLease(raw)
        pool = FakePool(lease)
        deadline = time.monotonic() + 3

        with patch("database.require_supported_runtime_backend", return_value="postgresql"):
            with patch("postgres_database.get_process_postgres_pool", return_value=pool) as get_pool:
                with patch("database.current_report_deadline", return_value=deadline):
                    connection = connect_database(":memory:", timeout=12)

        get_pool.assert_called_once_with(connect_timeout=12)
        self.assertEqual(pool.calls, [{}])
        self.assertTrue(
            any(
                isinstance(call[0], str) and "set_config('statement_timeout'" in call[0]
                for call in raw.calls
            )
        )
        self.assertFalse(raw.autocommit)
        connection.close()
        self.assertTrue(connection.closed)
        self.assertEqual(len(lease.exit_calls), 1)
        self.assertFalse(raw.autocommit)

    def test_postgresql_autocommit_mode_is_applied_only_when_requested(self):
        raw = FakeRawConnection()
        lease = FakeLease(raw)
        pool = FakePool(lease)

        with patch("database.require_supported_runtime_backend", return_value="postgresql"):
            with patch("postgres_database.get_process_postgres_pool", return_value=pool):
                with patch("database.current_report_deadline", return_value=None):
                    connection = connect_database(":memory:", isolation_level=None)

        self.assertTrue(raw.autocommit)
        connection.close()
        self.assertFalse(raw.autocommit)


if __name__ == "__main__":
    unittest.main()
