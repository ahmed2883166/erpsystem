"""Small, database-agnostic helpers for security-sensitive workflows."""

def login_attempts_exceeded(connection, username, remote_addr, cutoff_text,
                            username_limit=5, ip_limit=20):
    """Return True when either the account/IP or aggregate IP limit is reached."""
    attempts = connection.execute(
        """SELECT COUNT(*) FROM login_attempts
           WHERE username=? AND remote_addr=? AND attempted_at>=?""",
        (username, remote_addr, cutoff_text),
    ).fetchone()[0]
    ip_attempts = connection.execute(
        """SELECT COUNT(*) FROM login_attempts
           WHERE remote_addr=? AND attempted_at>=?""",
        (remote_addr, cutoff_text),
    ).fetchone()[0]
    return attempts >= username_limit or ip_attempts >= ip_limit


def linked_operational_source(connection, entry_id):
    """Return the first operational table referencing a journal entry, if any."""
    table_names = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )]
    for table_name in table_names:
        if table_name in {"journal_entries", "journal_lines"}:
            continue
        quoted = '"' + table_name.replace('"', '""') + '"'
        columns = {row[1] for row in connection.execute(
            "PRAGMA table_info(" + quoted + ")"
        )}
        # Operational modules use both the canonical name and qualified
        # names such as reversal_journal_entry_id / cancel_journal_entry_id.
        # Every such link must protect its document state from generic reversal.
        journal_columns = sorted(
            column for column in columns
            if column == "journal_entry_id" or column.endswith("_journal_entry_id")
        )
        for column in journal_columns:
            quoted_column = '"' + column.replace('"', '""') + '"'
            if connection.execute(
                "SELECT 1 FROM " + quoted + " WHERE " + quoted_column + "=? LIMIT 1",
                (entry_id,),
            ).fetchone():
                return table_name
    return None
