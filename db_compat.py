"""Focused SQLite/PostgreSQL compatibility helpers for staged migration.

The helpers cover schema inspection, transaction boundaries, generated IDs,
and multi-statement DDL execution. They do not translate arbitrary application
SQL or select the database used by the Flask application.
"""
import re
import sqlite3
from functools import wraps

try:
    import psycopg
except ImportError:  # SQLite-only installations remain supported during migration.
    psycopg = None


DB_ERROR = (sqlite3.Error,) + ((psycopg.Error,) if psycopg is not None else ())
DB_INTEGRITY_ERROR = (sqlite3.IntegrityError,) + ((psycopg.IntegrityError,) if psycopg is not None else ())


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _identifier(value, label="identifier"):
    value = str(value or "")
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"Invalid SQL {label}: {value!r}")
    return value


def database_dialect(connection):
    """Return the supported connection dialect without importing psycopg."""
    if getattr(connection, "_nexus_postgres_connection", False):
        return "postgresql"
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
        rows = connection.execute(f'PRAGMA table_xinfo("{table_name}")').fetchall()
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




def table_column_info(connection, table_name, *, schema=None):
    """Return portable column metadata keyed by name (type and PK position)."""
    table_name = _identifier(table_name, "table name")
    dialect = database_dialect(connection)
    if dialect == "sqlite":
        if not table_exists(connection, table_name):
            return {}
        rows = connection.execute(f'PRAGMA table_xinfo("{table_name}")').fetchall()
        return {
            (row["name"] if isinstance(row, sqlite3.Row) else row[1]): {
                "name": row["name"] if isinstance(row, sqlite3.Row) else row[1],
                "type": row["type"] if isinstance(row, sqlite3.Row) else row[2],
                "pk": row["pk"] if isinstance(row, sqlite3.Row) else row[5],
                "generated": (row["hidden"] if isinstance(row, sqlite3.Row) else row[6]) in (2, 3),
            }
            for row in rows
        }

    schema_name = _identifier(schema, "schema name") if schema else None
    if schema_name:
        rows = connection.execute(
            """
            SELECT c.column_name AS name, c.data_type AS type,
                   COALESCE(k.ordinal_position, 0) AS pk,
                   (c.is_generated <> 'NEVER') AS generated
            FROM information_schema.columns AS c
            LEFT JOIN information_schema.key_column_usage AS k
              ON k.table_schema=c.table_schema AND k.table_name=c.table_name
             AND k.column_name=c.column_name
             AND EXISTS (
                 SELECT 1 FROM information_schema.table_constraints AS t
                 WHERE t.constraint_schema=k.constraint_schema
                   AND t.constraint_name=k.constraint_name
                   AND t.table_name=k.table_name
                   AND t.constraint_type='PRIMARY KEY'
             )
            WHERE c.table_schema=%s AND c.table_name=%s
            ORDER BY c.ordinal_position
            """,
            (schema_name, table_name),
        ).fetchall()
    else:
        rows = connection.execute(
            """
            SELECT c.column_name AS name, c.data_type AS type,
                   COALESCE(k.ordinal_position, 0) AS pk,
                   (c.is_generated <> 'NEVER') AS generated
            FROM information_schema.columns AS c
            LEFT JOIN information_schema.key_column_usage AS k
              ON k.table_schema=c.table_schema AND k.table_name=c.table_name
             AND k.column_name=c.column_name
             AND EXISTS (
                 SELECT 1 FROM information_schema.table_constraints AS t
                 WHERE t.constraint_schema=k.constraint_schema
                   AND t.constraint_name=k.constraint_name
                   AND t.table_name=k.table_name
                   AND t.constraint_type='PRIMARY KEY'
             )
            WHERE c.table_schema=current_schema() AND c.table_name=%s
            ORDER BY c.ordinal_position
            """,
            (table_name,),
        ).fetchall()
    return {
        (row["name"] if isinstance(row, dict) else row[0]): {
            "name": row["name"] if isinstance(row, dict) else row[0],
            "type": row["type"] if isinstance(row, dict) else row[1],
            "pk": row["pk"] if isinstance(row, dict) else row[2],
            "generated": row["generated"] if isinstance(row, dict) else row[3],
        }
        for row in rows
    }


def table_indexes(connection, *, schema=None):
    """Return index names visible in the current SQLite/PostgreSQL schema."""
    dialect = database_dialect(connection)
    if dialect == "sqlite":
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' ORDER BY name"
        ).fetchall()
        return {row["name"] if isinstance(row, sqlite3.Row) else row[0] for row in rows}

    schema_name = _identifier(schema, "schema name") if schema else None
    if schema_name:
        rows = connection.execute(
            "SELECT indexname FROM pg_catalog.pg_indexes "
            "WHERE schemaname=%s ORDER BY indexname",
            (schema_name,),
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT indexname FROM pg_catalog.pg_indexes "
            "WHERE schemaname=current_schema() ORDER BY indexname"
        ).fetchall()
    return {row["indexname"] if isinstance(row, dict) else row[0] for row in rows}


def table_names(connection, *, schema=None):
    """Return base-table names visible in the current SQLite/PostgreSQL schema."""
    dialect = database_dialect(connection)
    if dialect == "sqlite":
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return {row["name"] if isinstance(row, sqlite3.Row) else row[0] for row in rows}

    schema_name = _identifier(schema, "schema name") if schema else None
    if schema_name:
        rows = connection.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=%s AND table_type='BASE TABLE' ORDER BY table_name",
            (schema_name,),
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=current_schema() AND table_type='BASE TABLE' "
            "ORDER BY table_name"
        ).fetchall()
    return {row["table_name"] if isinstance(row, dict) else row[0] for row in rows}



def _split_sql_statements(script):
    """Split PostgreSQL SQL on semicolons outside strings, identifiers and comments."""
    statements = []
    current = []
    i = 0
    state = "normal"
    block_depth = 0
    dollar_tag = None
    length = len(script)
    while i < length:
        char = script[i]
        nxt = script[i + 1] if i + 1 < length else ""
        if state in {"single", "escape_single"}:
            current.append(char)
            if char == "'" and nxt == "'":
                current.append(nxt)
                i += 2
                continue
            if state == "escape_single" and char == "\\" and nxt:
                current.append(nxt)
                i += 2
                continue
            if char == "'":
                state = "normal"
        elif state == "double":
            current.append(char)
            if char == '"' and nxt == '"':
                current.append(nxt)
                i += 2
                continue
            if char == '"':
                state = "normal"
        elif state == "line_comment":
            current.append(char)
            if char == "\n":
                state = "normal"
        elif state == "block_comment":
            if char == "/" and nxt == "*":
                current.extend((char, nxt))
                block_depth += 1
                i += 2
                continue
            if char == "*" and nxt == "/":
                current.extend((char, nxt))
                block_depth -= 1
                i += 2
                if block_depth == 0:
                    state = "normal"
                continue
            current.append(char)
        elif state == "dollar":
            if script.startswith(dollar_tag, i):
                current.append(dollar_tag)
                i += len(dollar_tag)
                state = "normal"
                continue
            current.append(char)
        else:
            if char == "'":
                previous_is_e = (
                    i > 0 and script[i - 1] in "eE"
                    and (i < 2 or not (script[i - 2].isalnum() or script[i - 2] == "_"))
                )
                state = "escape_single" if previous_is_e else "single"
                current.append(char)
            elif char == '"':
                state = "double"
                current.append(char)
            elif char == "-" and nxt == "-":
                current.extend((char, nxt))
                i += 2
                state = "line_comment"
                continue
            elif char == "/" and nxt == "*":
                current.extend((char, nxt))
                i += 2
                block_depth = 1
                state = "block_comment"
                continue
            elif char == "$" and (i == 0 or not (script[i - 1].isalnum() or script[i - 1] in "_$")):
                match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", script[i:])
                if match:
                    dollar_tag = match.group(0)
                    current.append(dollar_tag)
                    i += len(dollar_tag)
                    state = "dollar"
                    continue
                current.append(char)
            elif char == ";":
                statement = "".join(current).strip()
                if statement:
                    statements.append(statement)
                current = []
            else:
                current.append(char)
        i += 1
    if state in {"single", "escape_single", "double", "block_comment", "dollar"}:
        raise ValueError("Unterminated quoted SQL or comment in script.")
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def execute_script(connection, script):
    """Execute a multi-statement DDL script without SQLite-only executescript API."""
    if database_dialect(connection) == "sqlite":
        connection.executescript(script)
        return
    from db_sql import adapt_sql_for_postgresql
    for statement in _split_sql_statements(str(script)):
        connection.execute(adapt_sql_for_postgresql(statement))


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


def transactional_migration(function):
    """Give a schema/data migration an explicit transaction when it owns one.

    Nested calls reuse the caller's transaction and never commit it. On failure,
    an owned transaction is rolled back before the exception is propagated.
    """
    @wraps(function)
    def wrapped(connection, *args, **kwargs):
        owns_transaction = begin_write_transaction(connection)
        try:
            result = function(connection, *args, **kwargs)
            if owns_transaction:
                connection.commit()
            return result
        except Exception:
            if owns_transaction:
                connection.rollback()
            raise
    return wrapped


def insert_and_get_id(connection, sql, params=()):
    """Insert one row and return its generated id across SQLite and PostgreSQL."""
    statement = str(sql).strip().rstrip(";")
    if re.search(r"\bRETURNING\b", statement, re.IGNORECASE):
        raise ValueError("insert_and_get_id adds RETURNING id automatically; do not supply RETURNING.")
    if database_dialect(connection) == "sqlite":
        value = connection.execute(statement, params).lastrowid
    else:
        from db_sql import execute_compat
        row = execute_compat(
            connection, statement + " RETURNING id", params
        ).fetchone()
        if row is None:
            raise RuntimeError("INSERT ... RETURNING id did not return a row.")
        value = row["id"] if isinstance(row, dict) else row[0]
    if value is None:
        raise RuntimeError("The inserted row did not return a generated id.")
    return int(value)


def transaction_is_active(connection):
    """Return whether the DB-API connection currently owns a transaction."""
    if database_dialect(connection) == "sqlite":
        return connection.in_transaction
    status = getattr(connection.info, "transaction_status", None)
    return getattr(status, "name", str(status)) != "IDLE"
