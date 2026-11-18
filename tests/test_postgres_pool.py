"""Contract tests for the isolated PostgreSQL pool lifecycle."""
from contextlib import contextmanager
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest.mock import patch

from postgres_database import create_postgres_pool


class FakeConnectionPool:
    instances = []
    fail_open = False

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.opened = False
        self.closed = False
        self.wait_calls = []
        self.connection_calls = []
        self.instances.append(self)

    def open(self, *, wait, timeout):
        self.opened = True
        if self.fail_open:
            raise RuntimeError("pool startup failed")

    @contextmanager
    def connection(self, *, timeout):
        self.connection_calls.append(timeout)
        connection = SimpleNamespace(name="leased-connection")
        try:
            yield connection
        finally:
            self.returned_connection = connection

    def wait(self, *, timeout):
        self.wait_calls.append(timeout)
        return None

    def get_stats(self):
        return {"pool_size": 2, "pool_available": 1}

    def close(self):
        self.closed = True


def fake_pool_modules():
    psycopg_module = ModuleType("psycopg")
    psycopg_module.__path__ = []
    rows_module = ModuleType("psycopg.rows")
    rows_module.dict_row = object()
    pool_module = ModuleType("psycopg_pool")
    pool_module.ConnectionPool = FakeConnectionPool
    return {
        "psycopg": psycopg_module,
        "psycopg.rows": rows_module,
        "psycopg_pool": pool_module,
    }


class PostgreSQLPoolLifecycleTests(unittest.TestCase):
    def setUp(self):
        FakeConnectionPool.instances.clear()
        FakeConnectionPool.fail_open = False

    def test_pool_opens_with_bounded_sizes_and_dict_rows(self):
        with patch.dict(sys.modules, fake_pool_modules()):
            pool = create_postgres_pool(
                "postgresql://example.invalid/nexus",
                min_size=2,
                max_size=7,
                timeout=3,
                connect_timeout=9,
                application_name="NEXUS test",
            )

        raw_pool = FakeConnectionPool.instances[-1]
        self.assertTrue(raw_pool.opened)
        self.assertEqual(raw_pool.kwargs["min_size"], 2)
        self.assertEqual(raw_pool.kwargs["max_size"], 7)
        self.assertEqual(raw_pool.kwargs["timeout"], 3.0)
        self.assertFalse(raw_pool.kwargs["open"])
        self.assertEqual(raw_pool.kwargs["kwargs"]["connect_timeout"], 9)
        self.assertEqual(raw_pool.kwargs["kwargs"]["application_name"], "NEXUS test")
        self.assertIsNotNone(raw_pool.kwargs["kwargs"]["row_factory"])
        self.assertFalse(pool.closed)
        pool.close()

    def test_connection_lease_is_context_managed_and_returned(self):
        with patch.dict(sys.modules, fake_pool_modules()):
            pool = create_postgres_pool("postgresql://example.invalid/nexus", timeout=4)

        with pool.connection() as connection:
            self.assertEqual(connection.name, "leased-connection")
        raw_pool = FakeConnectionPool.instances[-1]
        self.assertEqual(raw_pool.connection_calls, [4.0])
        self.assertIs(raw_pool.returned_connection, connection)
        pool.close()

    def test_pool_wait_stats_and_close_are_exposed_safely(self):
        with patch.dict(sys.modules, fake_pool_modules()):
            pool = create_postgres_pool("postgresql://example.invalid/nexus")

        raw_pool = FakeConnectionPool.instances[-1]
        pool.wait(timeout=2)
        self.assertEqual(raw_pool.wait_calls, [2.0])
        self.assertEqual(pool.get_stats()["pool_size"], 2)
        pool.close()
        pool.close()
        self.assertTrue(raw_pool.closed)
        self.assertEqual(pool.get_stats(), {})
        with self.assertRaisesRegex(RuntimeError, "pool is closed"):
            with pool.connection():
                pass
        with self.assertRaisesRegex(RuntimeError, "pool is closed"):
            pool.wait()

    def test_pool_closes_if_startup_fails(self):
        FakeConnectionPool.fail_open = True
        with patch.dict(sys.modules, fake_pool_modules()):
            with self.assertRaisesRegex(RuntimeError, "pool startup failed"):
                create_postgres_pool("postgresql://example.invalid/nexus")
        self.assertTrue(FakeConnectionPool.instances[-1].closed)

    def test_invalid_pool_sizes_are_rejected_before_pool_creation(self):
        with patch.dict(sys.modules, fake_pool_modules()):
            for kwargs in (
                {"min_size": 0},
                {"min_size": 4, "max_size": 3},
                {"min_size": True},
            ):
                with self.subTest(kwargs=kwargs):
                    with self.assertRaises(ValueError):
                        create_postgres_pool("postgresql://example.invalid/nexus", **kwargs)
        self.assertEqual(FakeConnectionPool.instances, [])

    def test_missing_dsn_is_rejected_without_fallback(self):
        with patch.dict("os.environ", {"NEXUS_POSTGRES_DSN": ""}):
            with self.assertRaisesRegex(RuntimeError, "DSN is required"):
                create_postgres_pool(None)
        self.assertEqual(FakeConnectionPool.instances, [])

    def test_non_finite_or_non_positive_pool_timeout_is_rejected(self):
        with patch.dict(sys.modules, fake_pool_modules()):
            for timeout in (0, -1, float("inf"), float("nan")):
                with self.subTest(timeout=timeout):
                    with self.assertRaises(ValueError):
                        create_postgres_pool(
                            "postgresql://example.invalid/nexus", timeout=timeout
                        )
        self.assertEqual(FakeConnectionPool.instances, [])


class PostgreSQLDirectConnectionTests(unittest.TestCase):
    def test_connection_factory_passes_explicit_driver_settings(self):
        from postgres_database import connect_postgres
        psycopg_module = ModuleType("psycopg")
        psycopg_module.__path__ = []
        sentinel = object()
        psycopg_module.connect = unittest.mock.Mock(return_value=sentinel)
        rows_module = ModuleType("psycopg.rows")
        rows_module.dict_row = object()
        with patch.dict(
            sys.modules, {"psycopg": psycopg_module, "psycopg.rows": rows_module}
        ):
            connection = connect_postgres(
                "postgresql://example.invalid/nexus",
                autocommit=True,
                connect_timeout=8,
                application_name="NEXUS direct test",
            )
        self.assertIs(connection, sentinel)
        args, kwargs = psycopg_module.connect.call_args
        self.assertEqual(args, ("postgresql://example.invalid/nexus",))
        self.assertTrue(kwargs["autocommit"])
        self.assertEqual(kwargs["connect_timeout"], 8)
        self.assertEqual(kwargs["application_name"], "NEXUS direct test")
        self.assertIs(kwargs["row_factory"], rows_module.dict_row)

    def test_invalid_connection_settings_are_rejected_before_driver_call(self):
        from postgres_database import connect_postgres
        for kwargs in (
            {"connect_timeout": 0},
            {"connect_timeout": 31},
            {"connect_timeout": True},
            {"application_name": ""},
            {"autocommit": "yes"},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises((TypeError, ValueError)):
                    connect_postgres(
                        "postgresql://example.invalid/nexus", **kwargs
                    )

POSTGRES_DSN = __import__("os").environ.get("NEXUS_TEST_POSTGRES_DSN", "").strip()


@unittest.skipUnless(POSTGRES_DSN, "NEXUS_TEST_POSTGRES_DSN is not configured")
class PostgreSQLPoolIntegrationTests(unittest.TestCase):
    def test_pool_leases_commit_and_rollback_before_reuse(self):
        import uuid

        from postgres_database import create_postgres_pool

        table = "pool_probe_" + uuid.uuid4().hex
        pool = create_postgres_pool(
            POSTGRES_DSN, min_size=1, max_size=1, timeout=10
        )
        try:
            with pool.connection() as connection:
                connection.execute(
                    f'CREATE TEMP TABLE "{table}" (value INTEGER NOT NULL)'
                )
                connection.execute(
                    f'INSERT INTO "{table}" (value) VALUES (%s)', (10,)
                )

            # A pool lease is returned only after its transaction is committed.
            with pool.connection() as connection:
                row = connection.execute(
                    f'SELECT value FROM "{table}"'
                ).fetchone()
                self.assertEqual(row["value"], 10)
            with self.assertRaisesRegex(RuntimeError, "force rollback"):
                with pool.connection() as connection:
                    connection.execute(
                        f'INSERT INTO "{table}" (value) VALUES (%s)', (20,)
                    )
                    raise RuntimeError("force rollback")

            with pool.connection() as connection:
                rows = connection.execute(
                    f'SELECT value FROM "{table}" ORDER BY value'
                ).fetchall()
                self.assertEqual([row["value"] for row in rows], [10])
        finally:
            pool.close()

    def test_pool_waits_for_initial_connections_and_reports_stats(self):
        from postgres_database import create_postgres_pool

        pool = create_postgres_pool(
            POSTGRES_DSN, min_size=1, max_size=2, timeout=10
        )
        try:
            stats = pool.get_stats()
            self.assertGreaterEqual(stats.get("pool_size", 0), 1)
            with pool.connection() as connection:
                self.assertEqual(
                    connection.execute("SELECT 1 AS ready").fetchone()["ready"], 1
                )
        finally:
            pool.close()


if __name__ == "__main__":
    unittest.main()
