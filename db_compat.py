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
    """Split SQL on semicolons outside quoted strings, identifiers and comments."""
    statements = []
    current = []
    i = 0
    state = "normal"
    dollar_tag = None
    length = len(script)
    while i < length:
        char = script[i]
        nxt = script[i + 1] if i + 1 < length else ""
        if state == "single":
            current.append(char)
            if char == "'" and nxt == "'":
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
            if char == "\\n":
                state = "normal"
        elif state == "block_comment":
            current.append(char)
            if char == "*" and nxt == "/":
                current.append(nxt)
                i += 2
                state = "normal"
                continue
        elif state == "dollar":
            if script.startswith(dollar_tag, i):
                current.append(dollar_tag)
                i += len(dollar_tag)
                state = "normal"
                continue
            current.append(char)
        else:
            if char == "'":
                state = "single"
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
                state = "block_comment"
                continue
            elif char == "$":
                match = re.match(r"\\$[A-Za-z_][A-Za-z0-9_]*\\$|\\$\\$", script[i:])
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
    if state in {"single", "double", "block_comment", "dollar"}:
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
    for statement in _split_sql_statements(str(script)):
        connection.execute(statement)


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


def insert_and_get_id(connection, sql, params=()):
    """Insert one row and return its generated id across SQLite and PostgreSQL."""
    statement = str(sql).strip().rstrip(";")
    if re.search(r"\bRETURNING\b", statement, re.IGNORECASE):
        raise ValueError("insert_and_get_id adds RETURNING id automatically; do not supply RETURNING.")
    if database_dialect(connection) == "sqlite":
        value = connection.execute(statement, params).lastrowid
    else:
        from db_sql import qmark_to_format
        row = connection.execute(qmark_to_format(statement + " RETURNING id"), params).fetchone()
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
