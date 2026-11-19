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
