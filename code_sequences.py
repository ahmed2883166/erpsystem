"""Atomic, per-entity human-readable number sequences for NEXUS ERP."""
import re

from db_compat import begin_write_transaction, database_dialect
from db_sql import execute_compat


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_PREFIX = re.compile(r"[A-Z][A-Z0-9]*\Z")


def _next_legacy_number(connection, table, column, prefix):
    """Return the next numeric suffix, ignoring malformed legacy codes.

    SQLite silently casts non-numeric suffixes to zero, while PostgreSQL raises
    an invalid-text-representation error. Parse candidate suffixes in Python
    so both engines make the same decision without lossy SQL casts.
    """
    rows = execute_compat(
        connection,
        f"SELECT {column} FROM {table} WHERE {column} LIKE ?",
        (prefix + "-%",),
    ).fetchall()
    last_number = 0
    marker = prefix + "-"
    for row in rows:
        value = row[column]
        if not isinstance(value, str) or not value.startswith(marker):
            continue
        suffix = value[len(marker):]
        if suffix.isascii() and suffix.isdigit():
            last_number = max(last_number, int(suffix))
    return last_number + 1


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
    elif dialect == "postgresql":
        # Flask blueprint connections may use autocommit=True. That setting
        # does not mean a transaction is active; explicitly begin one when
        # the connection is idle, and reuse a caller-owned transaction.
        begin_write_transaction(connection)
        transaction_status = getattr(connection.info, "transaction_status", None)
        status_name = getattr(transaction_status, "name", str(transaction_status))
        if status_name != "INTRANS":
            raise RuntimeError(
                "PostgreSQL code sequences require an active, usable transaction."
            )

    execute_compat(connection, """
        CREATE TABLE IF NOT EXISTS document_sequences (
            sequence_key TEXT PRIMARY KEY,
            next_value BIGINT NOT NULL CHECK(next_value > 0)
        )
    """)

    if dialect == "postgresql":
        sequence_column = execute_compat(connection, """
            SELECT data_type FROM information_schema.columns
            WHERE table_schema=current_schema() AND table_name='document_sequences'
              AND column_name='next_value'
        """).fetchone()
        if sequence_column and sequence_column["data_type"] != "bigint":
            # Upgrade legacy INTEGER sequence counters without changing their values.
            execute_compat(connection, """ALTER TABLE document_sequences
                ALTER COLUMN next_value TYPE BIGINT USING next_value::BIGINT""")

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
        # Keep the reservation ahead of codes inserted by older import paths
        # or administrators before the sequence row was introduced.
        number = max(int(row["next_value"]), _next_legacy_number(
            connection, table, column, prefix
        ))

    execute_compat(
        connection,
        "UPDATE document_sequences SET next_value=? WHERE sequence_key=?",
        (number + 1, sequence_key),
    )
    return f"{prefix}-{number:0{width}d}"
