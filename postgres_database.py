"""PostgreSQL connection pool and driver primitives used by NEXUS runtime.

The Flask application selects these connections only when PostgreSQL is
explicitly configured and its runtime approval flag is present. SQLite remains
available for legacy installations and isolated compatibility tests.
"""
from contextlib import contextmanager
import math
import os
import re


DEFAULT_CONNECT_TIMEOUT_SECONDS = 5
MAX_CONNECT_TIMEOUT_SECONDS = 30
DEFAULT_APPLICATION_NAME = "NEXUS ERP"
DEFAULT_POSTGRES_SCHEMA = "nexus"
DEFAULT_POOL_MIN_SIZE = 1
DEFAULT_POOL_MAX_SIZE = 10
DEFAULT_POOL_TIMEOUT_SECONDS = 5.0


def _connection_settings(
    dsn=None,
    *,
    autocommit=False,
    connect_timeout=DEFAULT_CONNECT_TIMEOUT_SECONDS,
    application_name=DEFAULT_APPLICATION_NAME,
    schema=None,
):
    """Validate shared PostgreSQL connection settings before opening anything."""
    resolved_dsn = str(dsn or os.environ.get("NEXUS_POSTGRES_DSN") or "").strip()
    if not resolved_dsn:
        raise RuntimeError(
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
    schema = str(schema or os.environ.get("NEXUS_POSTGRES_SCHEMA") or DEFAULT_POSTGRES_SCHEMA).strip()
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema):
        raise ValueError("schema must be a lowercase PostgreSQL identifier.")
    return resolved_dsn, autocommit, connect_timeout, application_name, schema


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
    schema=None,
):
    """Open one standalone PostgreSQL connection.

    This remains a low-level prototype, not a complete drop-in replacement for
    sqlite3. Its default row shape is compatible with sqlite3.Row named and
    positional indexing; SQL and lifecycle contracts still require migration.
    """
    dsn, autocommit, connect_timeout, application_name, schema = _connection_settings(
        dsn,
        autocommit=autocommit,
        connect_timeout=connect_timeout,
        application_name=application_name,
        schema=schema,
    )
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            "PostgreSQL support is optional; install psycopg[binary,pool]>=3.2,<4.0."
        ) from exc

    connection = psycopg.connect(
        dsn,
        autocommit=autocommit,
        row_factory=_load_driver_row_factory(row_factory),
        connect_timeout=connect_timeout,
        application_name=application_name,
        options=f"-c search_path={schema}",
    )
    try:
        row = connection.execute("SELECT current_schema() AS schema_name").fetchone()
        actual_schema = row["schema_name"] if isinstance(row, dict) else row[0]
        if actual_schema != schema:
            raise RuntimeError(
                f"Configured PostgreSQL schema {schema!r} is unavailable; "
                f"current_schema() resolved to {actual_schema!r}."
            )
        if not autocommit:
            connection.rollback()
        return connection
    except Exception:
        connection.close()
        raise


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
    schema=None,
):
    """Create and open a bounded PostgreSQL pool without selecting it at runtime.

    Pool acquisition timeout and server connection timeout are intentionally
    separate settings. The returned wrapper requires context-managed leases;
    it does not expose raw getconn()/putconn() operations.
    """
    dsn, autocommit, connect_timeout, application_name, schema = _connection_settings(
        dsn,
        autocommit=autocommit,
        connect_timeout=connect_timeout,
        application_name=application_name,
        schema=schema,
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

    def configure_connection(connection):
        row = connection.execute("SELECT current_schema() AS schema_name").fetchone()
        actual_schema = row["schema_name"] if isinstance(row, dict) else row[0]
        if actual_schema != schema:
            raise RuntimeError(
                f"Configured PostgreSQL schema {schema!r} is unavailable; "
                f"current_schema() resolved to {actual_schema!r}."
            )
        if not autocommit:
            connection.rollback()

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
            "options": f"-c search_path={schema}",
        },
        configure=configure_connection,
        open=False,
    )
    try:
        pool.open(wait=True, timeout=pool_timeout)
    except Exception:
        pool.close()
        raise
    return PostgresConnectionPool(pool, timeout=pool_timeout)


# One bounded pool per worker process. Runtime selection remains guarded in
# database_backend.py; this registry only supplies lifecycle-safe primitives.
import atexit
import threading

_PROCESS_POOL = None
_PROCESS_POOL_PID = None
_PROCESS_POOL_CONFIG = None
_PROCESS_POOL_LOCK = threading.RLock()


def _reset_process_pool_lock_after_fork():
    """A child must not inherit a lock potentially held by a vanished thread."""
    global _PROCESS_POOL_LOCK
    _PROCESS_POOL_LOCK = threading.RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_process_pool_lock_after_fork)


def _pool_env_int(name, default, minimum, maximum):
    raw = os.environ.get(name)
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _pool_env_timeout(name, default, minimum=0.1, maximum=60.0):
    raw = os.environ.get(name)
    try:
        value = float(raw) if raw is not None else default
    except (TypeError, ValueError):
        value = default
    if not math.isfinite(value):
        value = default
    return max(minimum, min(value, maximum))


def get_process_postgres_pool(dsn=None, **overrides):
    """Return the single bounded PostgreSQL pool owned by this worker process.

    DSN, schema, connection timeout, application name, and pool sizing are
    immutable for the lifetime of a worker. A forked child creates its own
    pool and never touches the parent's inherited pool or lock.
    """
    global _PROCESS_POOL, _PROCESS_POOL_PID, _PROCESS_POOL_CONFIG
    resolved_dsn = str(dsn or os.environ.get("NEXUS_POSTGRES_DSN") or "").strip()
    if not resolved_dsn:
        raise RuntimeError("PostgreSQL DSN is required to initialize the process pool.")
    min_size = overrides.pop("min_size", _pool_env_int(
        "NEXUS_PG_POOL_MIN_SIZE", DEFAULT_POOL_MIN_SIZE, 1, 10
    ))
    max_size = overrides.pop("max_size", _pool_env_int(
        "NEXUS_PG_POOL_MAX_SIZE", DEFAULT_POOL_MAX_SIZE, 1, 20
    ))
    timeout = overrides.pop("timeout", _pool_env_timeout(
        "NEXUS_PG_POOL_TIMEOUT_SECONDS", DEFAULT_POOL_TIMEOUT_SECONDS
    ))
    schema = overrides.pop(
        "schema", os.environ.get("NEXUS_POSTGRES_SCHEMA") or DEFAULT_POSTGRES_SCHEMA
    )
    connect_timeout = overrides.pop(
        "connect_timeout", _pool_env_int(
            "NEXUS_PG_CONNECT_TIMEOUT_SECONDS", DEFAULT_CONNECT_TIMEOUT_SECONDS, 1, 30
        )
    )
    application_name = overrides.pop(
        "application_name", os.environ.get("NEXUS_PG_APPLICATION_NAME") or DEFAULT_APPLICATION_NAME
    )
    if max_size < min_size:
        raise ValueError("NEXUS_PG_POOL_MAX_SIZE must be greater than or equal to the minimum.")
    if overrides:
        raise TypeError("Unsupported process pool options: " + ", ".join(sorted(overrides)))
    # Normalize and validate every connection-affecting setting before comparing.
    _, _, connect_timeout, application_name, schema = _connection_settings(
        resolved_dsn, autocommit=False, connect_timeout=connect_timeout,
        application_name=application_name, schema=schema,
    )
    config = (
        resolved_dsn, min_size, max_size, timeout, schema,
        connect_timeout, application_name,
    )
    pid = os.getpid()
    with _PROCESS_POOL_LOCK:
        if _PROCESS_POOL is not None and _PROCESS_POOL_PID == pid:
            if _PROCESS_POOL_CONFIG != config:
                raise RuntimeError(
                    "PostgreSQL pool settings changed after initialization; restart the worker."
                )
            if _PROCESS_POOL.closed:
                raise RuntimeError("The process PostgreSQL pool was closed; restart the worker.")
            return _PROCESS_POOL
        # A PID mismatch means this is a child after fork. Do not close the
        # inherited pool: its worker threads belong to the parent process.
        _PROCESS_POOL = create_postgres_pool(
            resolved_dsn, min_size=min_size, max_size=max_size, timeout=timeout,
            autocommit=False, schema=schema, connect_timeout=connect_timeout,
            application_name=application_name,
        )
        _PROCESS_POOL_PID = pid
        _PROCESS_POOL_CONFIG = config
        return _PROCESS_POOL


def close_process_postgres_pool():
    """Close the current process pool during orderly worker shutdown."""
    global _PROCESS_POOL, _PROCESS_POOL_PID, _PROCESS_POOL_CONFIG
    pid = os.getpid()
    with _PROCESS_POOL_LOCK:
        if _PROCESS_POOL is not None and _PROCESS_POOL_PID == pid:
            _PROCESS_POOL.close()
        _PROCESS_POOL = None
        _PROCESS_POOL_PID = None
        _PROCESS_POOL_CONFIG = None


atexit.register(close_process_postgres_pool)
