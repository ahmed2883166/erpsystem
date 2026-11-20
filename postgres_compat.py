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
    """DB-API adapter that returns a pool lease instead of closing its socket.

    Flask's existing teardown calls connection.close(). For SQLite that drops
    the request connection and rolls back any uncommitted work. This adapter
    preserves that contract while returning the borrowed PostgreSQL connection
    to psycopg_pool for reuse.
    """

    _nexus_postgres_connection = True

    def __init__(self, lease):
        self._lease = lease
        self._closed = False
        raw_connection = lease.__enter__()
        self._connection = PostgresCompatConnection(raw_connection)

    @property
    def closed(self):
        return self._closed

    def _ensure_open(self):
        if self._closed:
            raise RuntimeError("PostgreSQL pool lease is already returned.")

    def close(self):
        """Roll back unfinished work and return the lease exactly once."""
        if self._closed:
            return
        try:
            self._connection.rollback()
        except BaseException as exc:
            self._closed = True
            self._lease.__exit__(type(exc), exc, exc.__traceback__)
            raise
        self._closed = True
        self._lease.__exit__(None, None, None)

    def __enter__(self):
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """Match DB-API context semantics and release the pool lease."""
        if not self._closed:
            self._closed = True
            return self._lease.__exit__(exc_type, exc_value, traceback)
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
