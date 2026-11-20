"""Transparent qmark/SQLite-DDL adaptation for application PostgreSQL connections.

This wrapper lets existing connection.execute() and cursor.execute() call sites
use the same narrow compatibility adapter while modules are migrated. It does
not translate SQLite-only functions, PRAGMA, sqlite_master, or transaction
semantics; those still require explicit module-level work.
"""
from db_sql import adapt_sql_for_postgresql, qmark_to_format


def _adapt_statement(sql):
    return qmark_to_format(adapt_sql_for_postgresql(sql))


class PostgresCompatCursor:
    """Cursor proxy that adapts SQL before delegating to psycopg."""

    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, query, params=None):
        statement = _adapt_statement(query)
        if params is None:
            self._cursor.execute(statement)
        else:
            self._cursor.execute(statement, params)
        return self

    def executemany(self, query, params_seq):
        self._cursor.executemany(_adapt_statement(query), params_seq)
        return self

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def __iter__(self):
        return iter(self._cursor)

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._cursor.__exit__(exc_type, exc_value, traceback)


class PostgresCompatConnection:
    """DB-API connection proxy with transparent SQL adaptation."""

    _nexus_postgres_connection = True

    def __init__(self, connection):
        self._connection = connection

    def execute(self, query, params=None):
        statement = _adapt_statement(query)
        if params is None:
            return self._connection.execute(statement)
        return self._connection.execute(statement, params)

    def executemany(self, query, params_seq):
        cursor = self._connection.cursor()
        cursor.executemany(_adapt_statement(query), params_seq)
        return PostgresCompatCursor(cursor)

    def cursor(self, *args, **kwargs):
        return PostgresCompatCursor(self._connection.cursor(*args, **kwargs))

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._connection.__exit__(exc_type, exc_value, traceback)


class PooledPostgresCompatConnection:
    """DB-API adapter that returns a clean lease to the process-wide pool.

    Pool connections use autocommit=False as their baseline. A request may
    temporarily request SQLite-compatible autocommit=True; close always resets
    statement_timeout and restores the pool baseline before returning the lease.
    """

    _nexus_postgres_connection = True

    def __init__(self, lease, *, autocommit=False, statement_timeout_ms=None):
        self._lease = lease
        self._closed = False
        self._desired_autocommit = bool(autocommit)
        self._connection = None
        entered = False
        try:
            raw_connection = lease.__enter__()
            entered = True
            self._connection = PostgresCompatConnection(raw_connection)
            raw_connection.autocommit = True
            if statement_timeout_ms is not None:
                raw_connection.execute(
                    "SELECT set_config('statement_timeout', %s, false)",
                    (f"{max(1, int(statement_timeout_ms))}ms",),
                )
            raw_connection.autocommit = self._desired_autocommit
        except Exception as exc:
            self._closed = True
            if entered:
                lease.__exit__(type(exc), exc, exc.__traceback__)
            raise

    @property
    def closed(self):
        return self._closed

    def _ensure_open(self):
        if self._closed:
            raise RuntimeError("PostgreSQL pool lease is already returned.")

    def _reset_session(self):
        raw = self._connection._connection
        if not getattr(raw, "autocommit", False):
            raw.rollback()
        raw.autocommit = True
        raw.execute("RESET statement_timeout")
        raw.autocommit = False

    def close(self):
        """Roll back unfinished work, reset session state, and return once."""
        if self._closed:
            return
        error = None
        try:
            self._reset_session()
        except Exception as exc:
            error = exc
        self._closed = True
        if error is None:
            self._lease.__exit__(None, None, None)
        else:
            self._lease.__exit__(type(error), error, error.__traceback__)
            raise error

    def __enter__(self):
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self._closed:
            return False
        raw = self._connection._connection
        try:
            if exc_type is not None:
                raw.rollback()
            elif not getattr(raw, "autocommit", False):
                raw.commit()
            self._reset_session()
        except Exception as reset_error:
            self._closed = True
            self._lease.__exit__(type(reset_error), reset_error, reset_error.__traceback__)
            raise
        self._closed = True
        self._lease.__exit__(exc_type, exc_value, traceback)
        return False

    def __getattr__(self, name):
        self._ensure_open()
        return getattr(self._connection, name)

    def execute(self, query, params=None):
        self._ensure_open()
        return self._connection.execute(query, params)

    def executemany(self, query, params_seq):
        self._ensure_open()
        return self._connection.executemany(query, params_seq)

    def cursor(self, *args, **kwargs):
        self._ensure_open()
        return self._connection.cursor(*args, **kwargs)
