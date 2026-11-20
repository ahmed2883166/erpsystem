"""Tests for returning Flask-style PostgreSQL connections to the pool."""
import unittest

from postgres_compat import PooledPostgresCompatConnection


class FakeRawConnection:
    def __init__(self):
        self.calls = []

    def execute(self, query, params=None):
        self.calls.append(("execute", query, params))
        return "cursor-result"

    def executemany(self, query, params):
        self.calls.append(("executemany", query, list(params)))
        return "executemany-result"

    def cursor(self, *args, **kwargs):
        raise AssertionError("cursor not used by this test")

    def rollback(self):
        self.calls.append(("rollback",))

    def commit(self):
        self.calls.append(("commit",))


class FakeLease:
    def __init__(self, connection):
        self.connection = connection
        self.entered = False
        self.exit_calls = []

    def __enter__(self):
        self.entered = True
        return self.connection

    def __exit__(self, exc_type, exc_value, traceback):
        self.exit_calls.append((exc_type, exc_value, traceback))
        return False


class PooledPostgresCompatConnectionTests(unittest.TestCase):
    def setUp(self):
        self.raw = FakeRawConnection()
        self.lease = FakeLease(self.raw)
        self.connection = PooledPostgresCompatConnection(self.lease)

    def test_close_rolls_back_and_returns_lease_without_closing_socket(self):
        self.connection.execute("SELECT ? AS value", (7,))
        self.connection.close()

        self.assertEqual(
            self.raw.calls,
            [
                ("execute", "SELECT %s AS value", (7,)),
                ("rollback",),
            ],
        )
        self.assertEqual(len(self.lease.exit_calls), 1)
        self.assertIsNone(self.lease.exit_calls[0][0])
        self.assertTrue(self.connection.closed)

    def test_close_is_idempotent_and_does_not_return_lease_twice(self):
        self.connection.close()
        self.connection.close()

        self.assertEqual(self.raw.calls, [("rollback",)])
        self.assertEqual(len(self.lease.exit_calls), 1)

    def test_explicit_commit_is_preserved_then_close_releases_clean_lease(self):
        self.connection.commit()
        self.connection.close()

        self.assertEqual(self.raw.calls, [("commit",), ("rollback",)])
        self.assertEqual(len(self.lease.exit_calls), 1)

    def test_use_after_close_is_rejected(self):
        self.connection.close()
        with self.assertRaisesRegex(RuntimeError, "lease is already returned"):
            self.connection.execute("SELECT 1")

    def test_rollback_failure_is_forwarded_to_lease_exit(self):
        error = RuntimeError("rollback failed")

        def fail_rollback():
            raise error

        self.raw.rollback = fail_rollback
        with self.assertRaisesRegex(RuntimeError, "rollback failed"):
            self.connection.close()

        self.assertEqual(len(self.lease.exit_calls), 1)
        self.assertIs(self.lease.exit_calls[0][1], error)
        self.assertTrue(self.connection.closed)


if __name__ == "__main__":
    unittest.main()
