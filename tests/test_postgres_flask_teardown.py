"""Flask app-context teardown tests for pooled PostgreSQL connection leases."""

import importlib.util
import os
import unittest
import uuid

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


POSTGRES_DSN = os.environ.get("NEXUS_TEST_POSTGRES_DSN", "").strip()
POSTGRES_POOL_AVAILABLE = importlib.util.find_spec("psycopg_pool") is not None
try:
    import psycopg
    from psycopg import sql
except ImportError:
    psycopg = None
    sql = None


@unittest.skipUnless(
    POSTGRES_DSN and psycopg is not None and POSTGRES_POOL_AVAILABLE,
    "NEXUS_TEST_POSTGRES_DSN and psycopg_pool are required",
)
class RealPostgreSQLFlaskPooledTeardownTests(unittest.TestCase):
    def test_flask_teardown_rolls_back_and_returns_real_pool_lease(self):
        from postgres_database import create_postgres_pool

        schema = "nexus_flask_lease_" + uuid.uuid4().hex[:16]
        with psycopg.connect(POSTGRES_DSN, autocommit=True) as admin:
            admin.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
            )
            admin.execute(
                sql.SQL("CREATE TABLE {}.lease_probe (value INTEGER NOT NULL)").format(
                    sql.Identifier(schema)
                )
            )

        pool = None
        try:
            pool = create_postgres_pool(
                POSTGRES_DSN,
                schema=schema,
                min_size=1,
                max_size=1,
                timeout=10,
            )
            pool.wait(timeout=10)

            app = Flask(__name__)
            register_database_connection_teardown(app)
            with app.app_context():
                lease = pool.connection()
                connection = PooledPostgresCompatConnection(lease)
                _track_connection(connection)
                connection.execute(
                    "INSERT INTO lease_probe(value) VALUES (?)",
                    (7,),
                )
                self.assertFalse(connection.closed)

            self.assertTrue(connection.closed)
            with pool.connection() as borrowed:
                row = borrowed.execute(
                    "SELECT COUNT(*) FROM lease_probe"
                ).fetchone()
                self.assertEqual(row[0], 0)

            stats = pool.get_stats()
            self.assertGreaterEqual(stats.get("pool_available", 0), 1)
        finally:
            if pool is not None:
                pool.close()
            with psycopg.connect(POSTGRES_DSN, autocommit=True) as admin:
                admin.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        sql.Identifier(schema)
                    )
                )


if __name__ == "__main__":
    unittest.main()
