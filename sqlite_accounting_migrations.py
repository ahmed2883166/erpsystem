"""SQLite-only accounting table migrations kept out of Flask startup."""
from db_compat import database_dialect, table_column_info


def migrate_journal_line_amounts_to_text(connection):
    """Rebuild legacy journal_lines with decimal-safe TEXT amount columns.

    The migration copies every row and primary key before replacing the table.
    It is intentionally SQLite-only; PostgreSQL uses NUMERIC columns and must
    receive a separate native migration.
    """
    if database_dialect(connection) != "sqlite":
        raise RuntimeError(
            "journal_lines TEXT rebuild is SQLite-only; PostgreSQL uses NUMERIC migrations."
        )
    columns = table_column_info(connection, "journal_lines")
    debit_type = str(columns.get("debit", {}).get("type", "")).upper()
    credit_type = str(columns.get("credit", {}).get("type", "")).upper()
    if not columns:
        raise RuntimeError("journal_lines table must exist before its amount migration.")
    if debit_type == "TEXT" and credit_type == "TEXT":
        return False

    connection.execute("""CREATE TABLE journal_lines_decimal_new(
        id INTEGER PRIMARY KEY,
        entry_id INTEGER NOT NULL REFERENCES journal_entries(id) ON DELETE CASCADE,
        account_id INTEGER NOT NULL REFERENCES accounts(id),
        debit TEXT NOT NULL DEFAULT '0',
        credit TEXT NOT NULL DEFAULT '0',
        memo TEXT
    )""")
    connection.execute("""INSERT INTO journal_lines_decimal_new(id,entry_id,account_id,debit,credit,memo)
        SELECT id,entry_id,account_id,CAST(debit AS TEXT),CAST(credit AS TEXT),memo
        FROM journal_lines ORDER BY id""")
    connection.execute("DROP TABLE journal_lines")
    connection.execute("ALTER TABLE journal_lines_decimal_new RENAME TO journal_lines")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_journal_lines_entry ON journal_lines(entry_id)")
    return True
