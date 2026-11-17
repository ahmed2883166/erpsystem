"""Explicit helper for single-row inserts that need the generated primary key."""
import re

from db_compat import database_dialect
from db_sql import qmark_to_format


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def insert_one_returning_id(connection, insert_sql, params=(), *, id_column="id"):
    """Execute one INSERT and return its generated key on SQLite or psycopg.

    This helper is opt-in. It does not commit, and it expects an INSERT without
    an existing RETURNING clause. PostgreSQL uses RETURNING; SQLite uses the
    cursor's lastrowid. If INSERT ... ON CONFLICT DO NOTHING inserts no row,
    PostgreSQL returns None.
    """
    if not isinstance(insert_sql, str) or not insert_sql.lstrip().upper().startswith("INSERT "):
        raise ValueError("insert_sql must be a single INSERT statement")
    if not _IDENTIFIER.fullmatch(str(id_column or "")):
        raise ValueError("id_column must be a simple SQL identifier")
    if re.search(r"\bRETURNING\b", insert_sql, re.IGNORECASE):
        raise ValueError("insert_sql must not already contain RETURNING")

    dialect = database_dialect(connection)
    if dialect == "sqlite":
        cursor = connection.execute(insert_sql, params)
        return cursor.lastrowid

    statement = insert_sql.rstrip().rstrip(";")
    statement = qmark_to_format(statement)
    cursor = connection.execute(
        f'{statement} RETURNING "{id_column}"',
        params,
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return row[id_column] if isinstance(row, dict) else row[0]
