"""Explicit test-only Flask integration for the staged PostgreSQL pool.

This module is intentionally not imported by app.py. It can only use the
separate NEXUS_TEST_POSTGRES_DSN when NEXUS_POSTGRES_POOL_TEST_MODE=1, so a
company/runtime DSN can never be selected accidentally by this test harness.
"""
from database import _track_connection, register_database_connection_teardown
from postgres_compat import PooledPostgresCompatConnection
from postgres_database import create_postgres_pool


_EXTENSION_KEY = "nexus_postgres_test_pool"


def init_test_postgres_pool(
    app,
    *,
    environ=None,
    min_size=1,
    max_size=1,
    timeout=5.0,
    schema=None,
):
    """Attach one isolated PostgreSQL pool to a Flask app for explicit tests."""
    import os

    source = os.environ if environ is None else environ
    if str(source.get("NEXUS_POSTGRES_POOL_TEST_MODE", "")).strip() != "1":
        raise RuntimeError(
            "PostgreSQL app pool is test-only; set NEXUS_POSTGRES_POOL_TEST_MODE=1."
        )
    dsn = str(source.get("NEXUS_TEST_POSTGRES_DSN", "") or "").strip()
    if not dsn:
        raise RuntimeError(
            "NEXUS_TEST_POSTGRES_DSN is required; production DSNs are not accepted."
        )
    if _EXTENSION_KEY in app.extensions:
        raise RuntimeError("A PostgreSQL test pool is already registered on this app.")

    pool = create_postgres_pool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        timeout=timeout,
        schema=schema,
        application_name="NEXUS isolated Flask pool test",
    )
    app.extensions[_EXTENSION_KEY] = pool
    register_database_connection_teardown(app)
    return pool


def test_postgres_connection(app):
    """Borrow one pool lease and register it for normal Flask app-context cleanup."""
    pool = app.extensions.get(_EXTENSION_KEY)
    if pool is None:
        raise RuntimeError("No isolated PostgreSQL test pool is registered on this app.")
    connection = PooledPostgresCompatConnection(pool.connection())
    return _track_connection(connection)


def close_test_postgres_pool(app):
    """Close the app-scoped pool explicitly after its test/application lifecycle."""
    pool = app.extensions.pop(_EXTENSION_KEY, None)
    if pool is not None:
        pool.close()
