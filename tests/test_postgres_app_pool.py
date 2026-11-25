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


POSTGRES_DSN = __import__("os").environ.get("NEXUS_TEST_POSTGRES_DSN", "").strip()
try:
    import psycopg
    from psycopg import sql
    import psycopg_pool  # noqa: F401
except ImportError:
    psycopg = None
    sql = None


@unittest.skipUnless(
    POSTGRES_DSN and psycopg is not None,
    "NEXUS_TEST_POSTGRES_DSN and psycopg_pool are required",
)
class RealPostgreSQLFlaskPoolConcurrencyTests(unittest.TestCase):
    def test_32_flask_contexts_share_bounded_pool_and_return_every_lease(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        import uuid

        schema = "nexus_flask_pool_" + uuid.uuid4().hex[:16]
        with psycopg.connect(POSTGRES_DSN, autocommit=True) as admin:
            admin.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
            )

        app = Flask("nexus-postgres-pool-concurrency")
        pool = None
        try:
            pool = postgres_app_pool.init_test_postgres_pool(
                app,
                environ={
                    "NEXUS_POSTGRES_POOL_TEST_MODE": "1",
                    "NEXUS_TEST_POSTGRES_DSN": POSTGRES_DSN,
                    # A production DSN, if present, must never be selected.
                    "NEXUS_POSTGRES_DSN": "postgresql://must-not-be-used.invalid/production",
                },
                min_size=2,
                max_size=10,
                timeout=20,
                schema=schema,
            )
            caller_count = 32
            barrier = Barrier(caller_count)

            def query_once(_):
                barrier.wait(timeout=15)
                with app.app_context():
                    connection = postgres_app_pool.test_postgres_connection(app)
                    row = connection.execute(
                        "SELECT pg_backend_pid() AS backend_pid, pg_sleep(0.03)"
                    ).fetchone()
                    return int(row["backend_pid"])

            with ThreadPoolExecutor(max_workers=caller_count) as executor:
                backend_pids = list(executor.map(query_once, range(caller_count)))

            self.assertEqual(len(backend_pids), caller_count)
            self.assertTrue(all(pid > 0 for pid in backend_pids))
            self.assertLessEqual(len(set(backend_pids)), 10)

            stats = pool.get_stats()
            self.assertLessEqual(stats.get("pool_size", 0), 10)
            self.assertEqual(stats.get("requests_errors", 0), 0)
            self.assertGreaterEqual(stats.get("pool_available", 0), 1)
        finally:
            if pool is not None:
                postgres_app_pool.close_test_postgres_pool(app)
            with psycopg.connect(POSTGRES_DSN, autocommit=True) as admin:
                admin.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        sql.Identifier(schema)
                    )
                )


if __name__ == "__main__":
    unittest.main()
