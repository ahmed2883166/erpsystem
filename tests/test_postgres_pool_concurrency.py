"""Real PostgreSQL concurrency smoke test for the bounded NEXUS pool.

Exercises 32 simultaneous callers against a 10-connection pool. It uses only
read-only SELECT statements and is skipped unless CI provides a disposable
PostgreSQL DSN and psycopg_pool.
"""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

POSTGRES_DSN = os.environ.get("NEXUS_TEST_POSTGRES_DSN", "").strip()
try:
    import psycopg_pool  # noqa: F401
except ImportError:
    POSTGRES_POOL_AVAILABLE = False
else:
    POSTGRES_POOL_AVAILABLE = True


@unittest.skipUnless(
    POSTGRES_DSN and POSTGRES_POOL_AVAILABLE,
    "NEXUS_TEST_POSTGRES_DSN and psycopg_pool are required",
)
class PostgreSQLPoolConcurrencySmokeTests(unittest.TestCase):
    def test_32_simultaneous_callers_stay_within_10_database_connections(self):
        from postgres_database import create_postgres_pool

        caller_count = 32
        pool_limit = 10
        barrier = Barrier(caller_count)
        pool = create_postgres_pool(
            POSTGRES_DSN,
            min_size=2,
            max_size=pool_limit,
            timeout=20,
            connect_timeout=10,
            application_name="NEXUS pool concurrency smoke",
            schema="public",
        )

        def query_once(_):
            barrier.wait(timeout=15)
            with pool.connection(timeout=20) as connection:
                row = connection.execute(
                    "SELECT pg_backend_pid() AS backend_pid, pg_sleep(0.03)"
                ).fetchone()
                return int(row["backend_pid"])

        try:
            with ThreadPoolExecutor(max_workers=caller_count) as executor:
                backend_pids = list(executor.map(query_once, range(caller_count)))

            self.assertEqual(len(backend_pids), caller_count)
            self.assertTrue(all(pid > 0 for pid in backend_pids))
            self.assertLessEqual(len(set(backend_pids)), pool_limit)

            stats = pool.get_stats()
            self.assertLessEqual(stats.get("pool_size", 0), pool_limit)
            self.assertEqual(stats.get("requests_errors", 0), 0)
        finally:
            pool.close()


if __name__ == "__main__":
    unittest.main()
