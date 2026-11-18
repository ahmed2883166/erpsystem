"""Small, database-agnostic helpers for security-sensitive workflows."""

from db_compat import table_columns, table_names
from db_sql import execute_compat


def login_attempts_exceeded(connection, username, remote_addr, cutoff_text,
                            username_limit=5, ip_limit=20):
    """Return True when either the account/IP or aggregate IP limit is reached."""
    attempts = execute_compat(connection,
        """SELECT COUNT(*) FROM login_attempts
           WHERE username=? AND remote_addr=? AND attempted_at>=?""",
        (username, remote_addr, cutoff_text),
    ).fetchone()[0]
    ip_attempts = execute_compat(connection,
        """SELECT COUNT(*) FROM login_attempts
           WHERE remote_addr=? AND attempted_at>=?""",
        (remote_addr, cutoff_text),
    ).fetchone()[0]
    return attempts >= username_limit or ip_attempts >= ip_limit


def linked_operational_source(connection, entry_id):
    """Return the first operational table referencing a journal entry, if any.

    Schema inspection is delegated to db_compat so the same protection works
    with SQLite and PostgreSQL connections. PostgreSQL inspection is scoped to
    the connection's current schema/search_path.
    """
    for table_name in sorted(table_names(connection)):
        if table_name.startswith("sqlite_") or table_name in {
            "journal_entries", "journal_lines"
        }:
            continue
        columns = table_columns(connection, table_name)
        journal_columns = sorted(
            column for column in columns
            if column == "journal_entry_id" or column.endswith("_journal_entry_id")
        )
        # Identifiers originate in database metadata, but quote them defensively.
        quoted_table = '"' + table_name.replace('"', '""') + '"'
        for column in journal_columns:
            quoted_column = '"' + column.replace('"', '""') + '"'
            if execute_compat(connection,
                "SELECT 1 FROM " + quoted_table + " WHERE " +
                quoted_column + "=? LIMIT 1",
                (entry_id,),
            ).fetchone():
                return table_name
    return None
