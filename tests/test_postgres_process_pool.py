"""Process-scoped PostgreSQL pool lifecycle tests; no real database is opened."""
import os
import unittest
from unittest.mock import patch

import postgres_database


class FakePool:
    def __init__(self):
        self.closed = False
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        self.closed = True


class ProcessPostgresPoolTests(unittest.TestCase):
    def setUp(self):
        self.original = (
            postgres_database._PROCESS_POOL,
            postgres_database._PROCESS_POOL_PID,
            postgres_database._PROCESS_POOL_CONFIG,
        )
        postgres_database._PROCESS_POOL = None
        postgres_database._PROCESS_POOL_PID = None
        postgres_database._PROCESS_POOL_CONFIG = None

    def tearDown(self):
        (
            postgres_database._PROCESS_POOL,
            postgres_database._PROCESS_POOL_PID,
            postgres_database._PROCESS_POOL_CONFIG,
        ) = self.original

    def test_reuses_one_pool_for_same_process_and_configuration(self):
        pool = FakePool()
        with patch.dict(
            os.environ, {"NEXUS_POSTGRES_DSN": "postgresql://test/db"}, clear=True
        ), patch.object(
            postgres_database, "create_postgres_pool", return_value=pool
        ) as factory, patch.object(postgres_database.os, "getpid", return_value=123):
            first = postgres_database.get_process_postgres_pool()
            second = postgres_database.get_process_postgres_pool()
        self.assertIs(first, second)
        factory.assert_called_once()
        self.assertEqual(pool.close_calls, 0)

    def test_rejects_every_connection_and_pool_setting_change(self):
        changes = {
            "NEXUS_POSTGRES_DSN": "postgresql://other/db",
            "NEXUS_POSTGRES_SCHEMA": "another_schema",
            "NEXUS_PG_CONNECT_TIMEOUT_SECONDS": "6",
            "NEXUS_PG_APPLICATION_NAME": "NEXUS worker",
            "NEXUS_PG_POOL_MIN_SIZE": "2",
            "NEXUS_PG_POOL_MAX_SIZE": "11",
            "NEXUS_PG_POOL_TIMEOUT_SECONDS": "6.0",
        }
        for env_name, changed_value in changes.items():
            with self.subTest(setting=env_name):
                postgres_database._PROCESS_POOL = None
                postgres_database._PROCESS_POOL_PID = None
                postgres_database._PROCESS_POOL_CONFIG = None
                pool = FakePool()
                with patch.dict(
                    os.environ,
                    {"NEXUS_POSTGRES_DSN": "postgresql://test/db"},
                    clear=True,
                ), patch.object(
                    postgres_database, "create_postgres_pool", return_value=pool
                ) as factory, patch.object(
                    postgres_database.os, "getpid", return_value=123
                ):
                    postgres_database.get_process_postgres_pool()
                    os.environ[env_name] = changed_value
                    with self.assertRaisesRegex(RuntimeError, "settings changed"):
                        postgres_database.get_process_postgres_pool()
                    factory.assert_called_once()

    def test_rejects_explicit_pool_override_change(self):
        pool = FakePool()
        with patch.dict(
            os.environ, {"NEXUS_POSTGRES_DSN": "postgresql://test/db"}, clear=True
        ), patch.object(
            postgres_database, "create_postgres_pool", return_value=pool
        ), patch.object(postgres_database.os, "getpid", return_value=123):
            postgres_database.get_process_postgres_pool(min_size=2, max_size=8)
            with self.assertRaisesRegex(RuntimeError, "settings changed"):
                postgres_database.get_process_postgres_pool(min_size=3, max_size=8)

    def test_child_process_does_not_reuse_or_close_inherited_pool(self):
        inherited = FakePool()
        child = FakePool()
        postgres_database._PROCESS_POOL = inherited
        postgres_database._PROCESS_POOL_PID = 100
        postgres_database._PROCESS_POOL_CONFIG = (
            "postgresql://test/db", 1, 10, 5.0, "nexus", 5, "NEXUS ERP"
        )
        with patch.dict(
            os.environ, {"NEXUS_POSTGRES_DSN": "postgresql://test/db"}, clear=True
        ), patch.object(
            postgres_database, "create_postgres_pool", return_value=child
        ) as factory, patch.object(postgres_database.os, "getpid", return_value=200):
            result = postgres_database.get_process_postgres_pool()
        self.assertIs(result, child)
        factory.assert_called_once()
        self.assertEqual(inherited.close_calls, 0)

    def test_shutdown_closes_only_current_process_pool_once(self):
        pool = FakePool()
        postgres_database._PROCESS_POOL = pool
        postgres_database._PROCESS_POOL_PID = 123
        postgres_database._PROCESS_POOL_CONFIG = (
            "postgresql://test/db", 1, 10, 5.0, "nexus", 5, "NEXUS ERP"
        )
        with patch.object(postgres_database.os, "getpid", return_value=123):
            postgres_database.close_process_postgres_pool()
            postgres_database.close_process_postgres_pool()
        self.assertEqual(pool.close_calls, 1)
        self.assertIsNone(postgres_database._PROCESS_POOL)


if __name__ == "__main__":
    unittest.main()
