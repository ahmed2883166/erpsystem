"""Isolated PostgreSQL connection primitives for the staged NEXUS migration.

These primitives are deliberately not wired into the Flask application. SQLite
remains the only runtime database until schema, SQL, transaction, and
backup/restore behavior have been migrated and verified.
"""
from contextlib import contextmanager
import math
import os


DEFAULT_CONNECT_TIMEOUT_SECONDS = 5
MAX_CONNECT_TIMEOUT_SECONDS = 30
DEFAULT_APPLICATION_NAME = "NEXUS ERP"
DEFAULT_POOL_MIN_SIZE = 1
DEFAULT_POOL_MAX_SIZE = 10
DEFAULT_POOL_TIMEOUT_SECONDS = 5.0


def _connection_settings(
    dsn=None,
    *,
    autocommit=False,
    connect_timeout=DEFAULT_CONNECT_TIMEOUT_SECONDS,
    application_name=DEFAULT_APPLICATION_NAME,
):
    """Validate shared PostgreSQL connection settings before opening anything."""
    resolved_dsn = str(dsn or os.environ.get("NEXUS_POSTGRES_DSN") or "").strip()
    if not resolved_dsn:
        raise ValueError(
            "PostgreSQL DSN is required; set NEXUS_POSTGRES_DSN or pass dsn explicitly."
        )
    if not isinstance(autocommit, bool):
        raise TypeError("autocommit must be a bool.")
    if isinstance(connect_timeout, bool):
        raise ValueError("connect_timeout must be an integer from 1 to 30 seconds.")
    try:
        timeout_text = str(connect_timeout).strip()
        if not timeout_text.isdecimal():
            raise ValueError
        connect_timeout = int(timeout_text)
    except (TypeError, ValueError) as exc:
        raise ValueError("connect_timeout must be an integer from 1 to 30 seconds.") from exc
    if not 1 <= connect_timeout <= MAX_CONNECT_TIMEOUT_SECONDS:
        raise ValueError("connect_timeout must be between 1 and 30 seconds.")
    application_name = (
        DEFAULT_APPLICATION_NAME if application_name is None else str(application_name).strip()
    )
    if not application_name or len(application_name) > 63:
        raise ValueError("application_name must contain between 1 and 63 characters.")
    return resolved_dsn, autocommit, connect_timeout, application_name


class HybridRow(dict):
    """Dictionary row that also preserves sqlite3.Row-style numeric indexing."""

    __slots__ = ("_values",)

    def __init__(self, column_names, values):
        self._values = tuple(values)
        super().__init__(zip(column_names, self._values))

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        return super().__getitem__(key)


def hybrid_row_factory(cursor):
    """Build rows supporting both row["column"] and row[0] access patterns."""
    description = cursor.description or ()
    column_names = tuple(
        column.name if hasattr(column, "name") else column[0]
        for column in description
    )

    def make_row(values):
        return HybridRow(column_names, values)

    return make_row


def _load_driver_row_factory(row_factory):
    # sqlite3.Row supports both named and positional lookup. The default
    # PostgreSQL row factory must preserve that contract because existing ERP
    # modules use both row["field"] and row[0] while migration is in progress.
    return row_factory or hybrid_row_factory


def connect_postgres(
    dsn=None,
    *,
    autocommit=False,
    row_factory=None,
    connect_timeout=DEFAULT_CONNECT_TIMEOUT_SECONDS,
    application_name=DEFAULT_APPLICATION_NAME,
):
    """Open one standalone PostgreSQL connection.

    This remains a low-level prototype, not a complete drop-in replacement for
    sqlite3. Its default row shape is compatible with sqlite3.Row named and
    positional indexing; SQL and lifecycle contracts still require migration.
    """
    dsn, autocommit, connect_timeout, application_name = _connection_settings(
        dsn,
        autocommit=autocommit,
        connect_timeout=connect_timeout,
        application_name=application_name,
    )
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            "PostgreSQL support is optional; install psycopg[binary,pool]>=3.2,<4.0."
        ) from exc

    return psycopg.connect(
        dsn,
        autocommit=autocommit,
        row_factory=_load_driver_row_factory(row_factory),
        connect_timeout=connect_timeout,
        application_name=application_name,
    )


class PostgresConnectionPool:
    """Small lifecycle wrapper around psycopg_pool.ConnectionPool.

    A connection is borrowed only inside the connection() context manager and
    is returned to the pool when that context exits. The pool owns commit /
    rollback-on-exit behavior; callers must not retain a borrowed connection.
    """

    def __init__(self, pool, *, timeout=DEFAULT_POOL_TIMEOUT_SECONDS):
        self._pool = pool
        self._timeout = timeout
        self._closed = False

    @contextmanager
    def connection(self, *, timeout=None):
        """Borrow a connection and always return its lease to the pool."""
        if self._closed:
            raise RuntimeError("PostgreSQL connection pool is closed.")
        lease_timeout = self._timeout if timeout is None else _positive_timeout(
            timeout, "connection timeout"
        )
        with self._pool.connection(timeout=lease_timeout) as connection:
            yield connection

    def wait(self, *, timeout=None):
        """Wait until the pool has established its configured minimum size."""
        if self._closed:
            raise RuntimeError("PostgreSQL connection pool is closed.")
        wait_timeout = self._timeout if timeout is None else _positive_timeout(
            timeout, "pool wait timeout"
        )
        return self._pool.wait(timeout=wait_timeout)

    def get_stats(self):
        """Return a snapshot of psycopg_pool counters for diagnostics."""
        if self._closed:
            return {}
        return dict(self._pool.get_stats())

    @property
    def closed(self):
        return self._closed

    def close(self):
        """Close the pool once; borrowed leases are handled by psycopg_pool."""
        if not self._closed:
            self._pool.close()
            self._closed = True

    def __enter__(self):
        if self._closed:
            raise RuntimeError("PostgreSQL connection pool is closed.")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False


def _positive_timeout(value, label):
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive number.")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive number.") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{label} must be a positive number.")
    return result


def create_postgres_pool(
    dsn=None,
    *,
    min_size=DEFAULT_POOL_MIN_SIZE,
    max_size=DEFAULT_POOL_MAX_SIZE,
    timeout=DEFAULT_POOL_TIMEOUT_SECONDS,
    autocommit=False,
    row_factory=None,
    connect_timeout=DEFAULT_CONNECT_TIMEOUT_SECONDS,
    application_name=DEFAULT_APPLICATION_NAME,
):
    """Create and open a bounded PostgreSQL pool without selecting it at runtime.

    Pool acquisition timeout and server connection timeout are intentionally
    separate settings. The returned wrapper requires context-managed leases;
    it does not expose raw getconn()/putconn() operations.
    """
    dsn, autocommit, connect_timeout, application_name = _connection_settings(
        dsn,
        autocommit=autocommit,
        connect_timeout=connect_timeout,
        application_name=application_name,
    )
    if isinstance(min_size, bool) or not isinstance(min_size, int) or min_size < 1:
        raise ValueError("min_size must be a positive integer.")
    if isinstance(max_size, bool) or not isinstance(max_size, int) or max_size < min_size:
        raise ValueError("max_size must be an integer greater than or equal to min_size.")
    pool_timeout = _positive_timeout(timeout, "pool timeout")

    try:
        from psycopg_pool import ConnectionPool
    except ImportError as exc:
        raise RuntimeError(
            "PostgreSQL pooling is optional; install psycopg[binary,pool]>=3.2,<4.0."
        ) from exc

    pool = ConnectionPool(
        conninfo=dsn,
        min_size=min_size,
        max_size=max_size,
        timeout=pool_timeout,
        kwargs={
            "autocommit": autocommit,
            "row_factory": _load_driver_row_factory(row_factory),
            "connect_timeout": connect_timeout,
            "application_name": application_name,
        },
        open=False,
    )
    try:
        pool.open(wait=True, timeout=pool_timeout)
    except Exception:
        pool.close()
        raise
    return PostgresConnectionPool(pool, timeout=pool_timeout)
