"""Atomic, per-entity human-readable number sequences for NEXUS ERP."""
import re

from db_compat import database_dialect
from db_sql import execute_compat


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\\Z")
_PREFIX = re.compile(r"[A-Z][A-Z0-9]*\\Z")


def _next_legacy_number(connection, table, column, prefix):
    row = execute_compat(
        connection,
        f"SELECT MAX(CAST(SUBSTR({column}, ?) AS INTEGER)) AS last_no "
        f"FROM {table} WHERE {column} LIKE ?",
        (len(prefix) + 2, prefix + "-%"),
    ).fetchone()
    return int(row["last_no"] or 0) + 1


def next_code(connection, sequence_key, prefix, table, column, width=6):
    """Reserve the next code inside the caller's transaction.

    SQLite keeps its existing BEGIN IMMEDIATE writer serialization. PostgreSQL
    uses a row lock on the sequence record and an INSERT conflict guard for the
    first reservation. The caller must commit or roll back the surrounding
    insert transaction; this function never commits.
    """
    if not all(_IDENTIFIER.fullmatch(item or "") for item in (table, column)):
        raise ValueError("Invalid sequence table or column.")
    if not _PREFIX.fullmatch(prefix or ""):
        raise ValueError("Invalid sequence prefix.")
    if not isinstance(sequence_key, str) or not sequence_key:
        raise ValueError("sequence_key must be a non-empty string.")
    try:
        width = int(width)
    except (TypeError, ValueError) as exc:
        raise ValueError("width must be an integer from 1 to 18.") from exc
    if not 1 <= width <= 18:
        raise ValueError("width must be an integer from 1 to 18.")

    dialect = database_dialect(connection)
    if dialect == "sqlite":
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
    elif connection.autocommit:
        raise RuntimeError(
            "PostgreSQL code sequences require an explicit transaction; "
            "connect with autocommit=False."
        )

    execute_compat(connection, """
        CREATE TABLE IF NOT EXISTS document_sequences (
            sequence_key TEXT PRIMARY KEY,
            next_value INTEGER NOT NULL CHECK(next_value > 0)
        )
    """)

    lock_clause = " FOR UPDATE" if dialect == "postgresql" else ""
    row = execute_compat(
        connection,
        "SELECT next_value FROM document_sequences WHERE sequence_key=?" + lock_clause,
        (sequence_key,),
    ).fetchone()

    if row is None:
        first_number = _next_legacy_number(connection, table, column, prefix)
        if dialect == "postgresql":
            # Competing first reservations may both observe no row. The unique
            # key arbitrates the insert; the subsequent FOR UPDATE reads the
            # winner's value after any concurrent transaction commits.
            execute_compat(
                connection,
                "INSERT INTO document_sequences(sequence_key,next_value) VALUES(?,?) "
                "ON CONFLICT(sequence_key) DO NOTHING",
                (sequence_key, first_number),
            )
            row = execute_compat(
                connection,
                "SELECT next_value FROM document_sequences WHERE sequence_key=? FOR UPDATE",
                (sequence_key,),
            ).fetchone()
            number = int(row["next_value"])
        else:
            number = first_number
            execute_compat(
                connection,
                "INSERT INTO document_sequences(sequence_key,next_value) VALUES(?,?)",
                (sequence_key, number + 1),
            )
            return f"{prefix}-{number:0{width}d}"

    else:
        number = int(row["next_value"])

    execute_compat(
        connection,
        "UPDATE document_sequences SET next_value=? WHERE sequence_key=?",
        (number + 1, sequence_key),
    )
    return f"{prefix}-{number:0{width}d}"
