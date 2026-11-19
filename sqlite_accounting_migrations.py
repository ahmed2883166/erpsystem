"""SQLite-only accounting table migrations kept out of Flask startup."""
import re

from db_compat import database_dialect, table_column_info


def _row_value(row, key, index):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return row[index]


def migrate_journal_line_amounts_to_text(connection):
    """Convert legacy REAL amounts without discarding the live table definition.

    The original CREATE TABLE statement, every non-generated column, explicit
    index, trigger, constraint and foreign key is retained. PostgreSQL uses
    native NUMERIC columns and must never run this SQLite table rebuild.
    """
    if database_dialect(connection) != "sqlite":
        raise RuntimeError(
            "journal_lines REAL-to-TEXT rebuild is SQLite-only; "
            "PostgreSQL uses native NUMERIC columns."
        )

    column_info = table_column_info(connection, "journal_lines")
    if not column_info:
        raise RuntimeError("journal_lines table must exist before its amount migration.")
    real_columns = [
        name for name in ("debit", "credit")
        if str(column_info.get(name, {}).get("type", "")).upper() == "REAL"
    ]
    if not real_columns:
        return False

    table_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='journal_lines'"
    ).fetchone()
    original_sql = _row_value(table_row, "sql", 0) if table_row else None
    if not original_sql:
        raise RuntimeError("Cannot safely rebuild journal_lines: original table DDL is missing.")

    replacement_sql = str(original_sql)
    table_pattern = re.compile(
        r'(?is)^(\\s*CREATE\\s+TABLE\\s+(?:IF\\s+NOT\\s+EXISTS\\s+)?)'
        r'(?:"journal_lines"|\`journal_lines\`|\\[journal_lines\\]|journal_lines)(\\s*\\()'
    )
    replacement_sql, table_count = table_pattern.subn(
        r"\\g<1>journal_lines_decimal_new\\g<2>", replacement_sql, count=1
    )
    if table_count != 1:
        raise RuntimeError("Cannot safely identify the journal_lines table name in its DDL.")

    for column_name in real_columns:
        column_pattern = re.compile(
            r'(?i)((?:"' + re.escape(column_name) + r'"|\`' +
            re.escape(column_name) + r'\`|\\[' + re.escape(column_name) +
            r'\\]|' + re.escape(column_name) + r')\\s+)REAL\\b'
        )
        replacement_sql, changed = column_pattern.subn(r"\\g<1>TEXT", replacement_sql, count=1)
        if changed != 1:
            raise RuntimeError(
                f"Cannot safely change {column_name}:REAL in the original journal_lines DDL."
            )

    index_rows = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='journal_lines' "
        "AND sql IS NOT NULL ORDER BY name"
    ).fetchall()
    trigger_rows = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name='journal_lines' "
        "AND sql IS NOT NULL ORDER BY name"
    ).fetchall()
    index_sql = [_row_value(row, "sql", 0) for row in index_rows]
    trigger_sql = [_row_value(row, "sql", 0) for row in trigger_rows]

    # Copy every ordinary stored column. Generated/hidden columns are recreated
    # from their original DDL and are deliberately not inserted explicitly.
    info_rows = connection.execute('PRAGMA table_xinfo("journal_lines")').fetchall()
    copy_columns = [
        _row_value(row, "name", 1)
        for row in info_rows
        if int(_row_value(row, "hidden", 6) or 0) == 0
    ]
    if not copy_columns:
        raise RuntimeError("Cannot safely rebuild journal_lines without stored columns.")
    quoted_columns = ", ".join('"' + str(name).replace('"', '""') + '"' for name in copy_columns)

    connection.execute("SAVEPOINT nexus_journal_lines_decimal_migration")
    try:
        connection.execute(replacement_sql)
        connection.execute(
            f"INSERT INTO journal_lines_decimal_new ({quoted_columns}) "
            f"SELECT {quoted_columns} FROM journal_lines"
        )
        connection.execute("DROP TABLE journal_lines")
        connection.execute(
            "ALTER TABLE journal_lines_decimal_new RENAME TO journal_lines"
        )
        for statement in index_sql:
            connection.execute(statement)
        for statement in trigger_sql:
            connection.execute(statement)
        violations = connection.execute("PRAGMA foreign_key_check").fetchmany(10)
        if violations:
            raise RuntimeError(
                "journal_lines migration produced foreign-key violations; changes were rolled back."
            )
        connection.execute("RELEASE SAVEPOINT nexus_journal_lines_decimal_migration")
        return True
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT nexus_journal_lines_decimal_migration")
        connection.execute("RELEASE SAVEPOINT nexus_journal_lines_decimal_migration")
        raise
