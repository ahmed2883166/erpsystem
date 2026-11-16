"""Atomic, per-entity human-readable number sequences for NEXUS ERP."""
import re


def next_code(connection, sequence_key, prefix, table, column, width=6):
    """Reserve the next code inside the caller's transaction.

    BEGIN IMMEDIATE serializes writers before reading/updating the sequence.
    Existing codes are scanned once when a sequence is first introduced, so
    legacy records are preserved and numbering continues after their maximum.
    The caller must commit or roll back the surrounding insert transaction.
    """
    if not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item or "") for item in (table, column)):
        raise ValueError("Invalid sequence table or column.")
    if not re.fullmatch(r"[A-Z][A-Z0-9]*", prefix or ""):
        raise ValueError("Invalid sequence prefix.")
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS document_sequences (
            sequence_key TEXT PRIMARY KEY,
            next_value INTEGER NOT NULL CHECK(next_value > 0)
        )
    """)
    row = connection.execute(
        "SELECT next_value FROM document_sequences WHERE sequence_key=?",
        (sequence_key,),
    ).fetchone()
    if row is None:
        existing = connection.execute(
            f"SELECT MAX(CAST(SUBSTR({column}, ?) AS INTEGER)) AS last_no "
            f"FROM {table} WHERE {column} LIKE ?",
            (len(prefix) + 2, prefix + "-%"),
        ).fetchone()
        number = int(existing["last_no"] or 0) + 1
        connection.execute(
            "INSERT INTO document_sequences(sequence_key,next_value) VALUES(?,?)",
            (sequence_key, number + 1),
        )
    else:
        number = int(row["next_value"])
        connection.execute(
            "UPDATE document_sequences SET next_value=? WHERE sequence_key=?",
            (number + 1, sequence_key),
        )
    return f"{prefix}-{number:0{width}d}"
