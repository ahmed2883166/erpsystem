"""Flask app-context teardown tests for pooled PostgreSQL connection leases."""

import unittest

from flask import Flask

from database import _track_connection, register_database_connection_teardown
from postgres_compat import PooledPostgresCompatConnection


class FakeRawConnection:
    def __init__(self):
        self.rollback_calls = 0

    def rollback(self):
        self.rollback_calls += 1


class FakeLease:
    def __init__(self, connection):
        self.connection = connection
        self.exit_calls = []

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc_value, traceback):
        self.exit_calls.append((exc_type, exc_value, traceback))
        return False


class FlaskPooledConnectionTeardownTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        register_database_connection_teardown(self.app)
        self.raw = FakeRawConnection()
        self.lease = FakeLease(self.raw)
        self.connection = PooledPostgresCompatConnection(self.lease)

    def test_app_context_teardown_returns_pooled_lease_once(self):
        with self.app.app_context():
            _track_connection(self.connection)
            self.assertFalse(self.connection.closed)
            self.assertEqual(self.lease.exit_calls, [])

        self.assertTrue(self.connection.closed)
        self.assertEqual(self.raw.rollback_calls, 1)
        self.assertEqual(len(self.lease.exit_calls), 1)
        self.assertIsNone(self.lease.exit_calls[0][0])

    def test_repeated_teardown_registration_does_not_duplicate_hook(self):
        register_database_connection_teardown(self.app)
        with self.app.app_context():
            _track_connection(self.connection)

        self.assertEqual(self.raw.rollback_calls, 1)
        self.assertEqual(len(self.lease.exit_calls), 1)


if __name__ == "__main__":
    unittest.main()
