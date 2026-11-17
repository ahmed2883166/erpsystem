"""Small, explicit cross-database schema inspection primitives.

This module is intentionally narrower than a DB-API compatibility layer. It
supports only metadata reads needed by application code; it does not translate
SQL, alter transaction behavior, or select a database for the application.
"""
import re
import sqlite3


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _identifier(value, label="identifier"):
    value = str(value or "")
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"Invalid SQL {label}: {value!r}")
    return value


def database_dialect(connection):
    """Return the supported connection dialect without importing psycopg."""
    if isinstance(connection, sqlite3.Connection):
        return "sqlite"
    module = connection.__class__.__module__.split(".", 1)[0]
    if module == "psycopg":
        return "postgresql"
    raise TypeError(f"Unsupported database connection type: {type(connection)!r}")


def table_exists(connection, table_name, *, schema=None):
    """Check for a table in SQLite or the selected PostgreSQL schema."""
    table_name = _identifier(table_name, "table name")
    dialect = database_dialect(connection)
    if dialect == "sqlite":
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table_name,),
        ).fetchone()
        return row is not None

    schema_name = _identifier(schema, "schema name") if schema else None
    if schema_name:
        row = connection.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema=%s AND table_name=%s AND table_type='BASE TABLE') AS present",
            (schema_name, table_name),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema=current_schema() AND table_name=%s "
            "AND table_type='BASE TABLE') AS present",
            (table_name,),
        ).fetchone()
    return bool(row["present"] if isinstance(row, dict) else row[0])


def table_columns(connection, table_name, *, schema=None):
    """Return the set of column names for a table, or an empty set if absent."""
    table_name = _identifier(table_name, "table name")
    dialect = database_dialect(connection)
    if dialect == "sqlite":
        if not table_exists(connection, table_name):
            return set()
        # The identifier was validated above; SQLite PRAGMA cannot bind it.
        rows = connection.execute(f'PRAGMA table_info("{table_name}")').fetchall()
        return {row["name"] if isinstance(row, sqlite3.Row) else row[1] for row in rows}

    schema_name = _identifier(schema, "schema name") if schema else None
    if schema_name:
        rows = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
            (schema_name, table_name),
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name=%s "
            "ORDER BY ordinal_position",
            (table_name,),
        ).fetchall()
    return {row["column_name"] if isinstance(row, dict) else row[0] for row in rows}


def begin_write_transaction(connection):
    """Start a write transaction without nesting an existing transaction.

    SQLite keeps BEGIN IMMEDIATE. Psycopg starts BEGIN only while idle; an
    existing transaction remains owned by its caller.
    """
    dialect = database_dialect(connection)
    if dialect == "sqlite":
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
            return True
        return False

    transaction_status = getattr(connection.info, "transaction_status", None)
    status_name = getattr(transaction_status, "name", str(transaction_status))
    if status_name == "IDLE":
        connection.execute("BEGIN")
        return True
    return False


def transaction_is_active(connection):
    """Return whether the DB-API connection currently owns a transaction."""
    if database_dialect(connection) == "sqlite":
        return connection.in_transaction
    status = getattr(connection.info, "transaction_status", None)
    return getattr(status, "name", str(status)) != "IDLE"
