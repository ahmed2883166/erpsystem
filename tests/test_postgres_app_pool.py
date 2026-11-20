"""Tests for the explicit, isolated Flask PostgreSQL pool harness."""
import unittest
from contextlib import contextmanager
from flask import Flask
from unittest.mock import patch

import postgres_app_pool


class FakeRawConnection:
    def __init__(self):
        self.rollback_calls = 0
        self.execute_calls = []

    def execute(self, statement, params=None):
        self.execute_calls.append((statement, params))
        return self

    def rollback(self):
        self.rollback_calls += 1


class FakePool:
    def __init__(self):
        self.closed = False
        self.leases = []

    @contextmanager
    def connection(self):
        raw = FakeRawConnection()
        self.leases.append(raw)
        try:
            yield raw
        finally:
            pass

    def close(self):
        self.closed = True


class FlaskPostgresPoolHarnessTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.pool = FakePool()
        self.env = {
            "NEXUS_POSTGRES_POOL_TEST_MODE": "1",
            "NEXUS_TEST_POSTGRES_DSN": "postgresql://isolated.invalid/test",
            # Deliberately present: this must never be selected by the harness.
            "NEXUS_POSTGRES_DSN": "postgresql://company.invalid/production",
        }

    def test_test_mode_and_separate_dsn_are_mandatory(self):
        with self.assertRaisesRegex(RuntimeError, "test-only"):
            postgres_app_pool.init_test_postgres_pool(
                self.app, environ={"NEXUS_TEST_POSTGRES_DSN": self.env["NEXUS_TEST_POSTGRES_DSN"]}
            )
        with self.assertRaisesRegex(RuntimeError, "NEXUS_TEST_POSTGRES_DSN"):
            postgres_app_pool.init_test_postgres_pool(
                self.app, environ={"NEXUS_POSTGRES_POOL_TEST_MODE": "1"}
            )

    def test_pool_is_app_scoped_and_uses_only_test_dsn(self):
        with patch.object(postgres_app_pool, "create_postgres_pool", return_value=self.pool) as create:
            registered = postgres_app_pool.init_test_postgres_pool(
                self.app, environ=self.env, min_size=1, max_size=2
            )
        self.assertIs(registered, self.pool)
        self.assertIs(self.app.extensions["nexus_postgres_test_pool"], self.pool)
        self.assertEqual(create.call_args.args[0], self.env["NEXUS_TEST_POSTGRES_DSN"])
        self.assertNotIn(self.env["NEXUS_POSTGRES_DSN"], create.call_args.args)

    def test_flask_context_teardown_returns_lease_and_pool_closes_explicitly(self):
        with patch.object(postgres_app_pool, "create_postgres_pool", return_value=self.pool):
            postgres_app_pool.init_test_postgres_pool(self.app, environ=self.env)
        with self.app.app_context():
            connection = postgres_app_pool.test_postgres_connection(self.app)
            connection.execute("SELECT 1")
            self.assertFalse(connection.closed)
        self.assertTrue(connection.closed)
        self.assertEqual(self.pool.leases[0].rollback_calls, 1)
        self.assertFalse(self.pool.closed)
        postgres_app_pool.close_test_postgres_pool(self.app)
        self.assertTrue(self.pool.closed)
        self.assertNotIn("nexus_postgres_test_pool", self.app.extensions)

    def test_missing_registration_and_duplicate_registration_fail_closed(self):
        with self.assertRaisesRegex(RuntimeError, "No isolated"):
            postgres_app_pool.test_postgres_connection(self.app)
        with patch.object(postgres_app_pool, "create_postgres_pool", return_value=self.pool):
            postgres_app_pool.init_test_postgres_pool(self.app, environ=self.env)
            with self.assertRaisesRegex(RuntimeError, "already registered"):
                postgres_app_pool.init_test_postgres_pool(self.app, environ=self.env)
        postgres_app_pool.close_test_postgres_pool(self.app)


if __name__ == "__main__":
    unittest.main()
