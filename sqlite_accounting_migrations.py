"""SQLite-only accounting and currency precision migrations."""
import re

from db_compat import database_dialect, table_column_info


def _row_value(row, key, index):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return row[index]


def _rebuild_real_columns_as_text(connection, table_name, column_names):
    """Rebuild one SQLite table while preserving its complete DDL and dependents."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(table_name)):
        raise ValueError("Invalid SQLite migration table name.")
    column_info = table_column_info(connection, table_name)
    if not column_info:
        raise RuntimeError(f"{table_name} table must exist before its amount migration.")
    real_columns = [
        name for name in column_names
        if str(column_info.get(name, {}).get("type", "")).upper() == "REAL"
    ]
    if not real_columns:
        return False

    table_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    original_sql = _row_value(table_row, "sql", 0) if table_row else None
    if not original_sql:
        raise RuntimeError(f"Cannot safely rebuild {table_name}: original table DDL is missing.")

    temp_table = f"{table_name}_decimal_new"
    replacement_sql = str(original_sql)
    table_pattern = re.compile(
        r'(?is)^(\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?)'
        r'(?:"' + re.escape(table_name) + r'"|\[' + re.escape(table_name) +
        r'\]|' + re.escape(table_name) + r')(\s*\()'
    )
    replacement_sql, table_count = table_pattern.subn(
        lambda match: match.group(1) + temp_table + match.group(2),
        replacement_sql,
        count=1,
    )
    if table_count != 1:
        raise RuntimeError(f"Cannot safely identify {table_name} in its original DDL.")

    for column_name in real_columns:
        column_pattern = re.compile(
            r'(?i)((?:"' + re.escape(column_name) + r'"|\[' +
            re.escape(column_name) + r'\]|' + re.escape(column_name) +
            r')\s+)REAL\b'
        )
        replacement_sql, changed = column_pattern.subn(r"\g<1>TEXT", replacement_sql, count=1)
        if changed != 1:
            raise RuntimeError(
                f"Cannot safely change {table_name}.{column_name} REAL declaration."
            )

    index_rows = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? "
        "AND sql IS NOT NULL ORDER BY name",
        (table_name,),
    ).fetchall()
    trigger_rows = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name=? "
        "AND sql IS NOT NULL ORDER BY name",
        (table_name,),
    ).fetchall()
    index_sql = [_row_value(row, "sql", 0) for row in index_rows]
    trigger_sql = [_row_value(row, "sql", 0) for row in trigger_rows]

    info_rows = connection.execute(
        f'PRAGMA table_xinfo("{table_name}")'
    ).fetchall()
    copy_columns = [
        _row_value(row, "name", 1)
        for row in info_rows
        if int(_row_value(row, "hidden", 6) or 0) == 0
    ]
    if not copy_columns:
        raise RuntimeError(f"Cannot safely rebuild {table_name} without stored columns.")
    quoted_columns = ", ".join('"' + str(name).replace('"', '""') + '"' for name in copy_columns)

    savepoint = "nexus_" + table_name + "_decimal_migration"
    connection.execute(f"SAVEPOINT {savepoint}")
    try:
        connection.execute(replacement_sql)
        connection.execute(
            f'INSERT INTO "{temp_table}" ({quoted_columns}) '
            f'SELECT {quoted_columns} FROM "{table_name}"'
        )
        connection.execute(f'DROP TABLE "{table_name}"')
        connection.execute(f'ALTER TABLE "{temp_table}" RENAME TO "{table_name}"')
        for statement in index_sql:
            connection.execute(statement)
        for statement in trigger_sql:
            connection.execute(statement)
        violations = connection.execute("PRAGMA foreign_key_check").fetchmany(10)
        if violations:
            raise RuntimeError(
                f"{table_name} migration produced foreign-key violations; changes were rolled back."
            )
        connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        return True
    except Exception:
        connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise


def migrate_journal_line_amounts_to_text(connection):
    """Convert legacy journal amounts to exact decimal text without losing dimensions."""
    if database_dialect(connection) != "sqlite":
        raise RuntimeError(
            "journal_lines REAL-to-TEXT rebuild is SQLite-only; "
            "PostgreSQL uses native NUMERIC columns."
        )
    return _rebuild_real_columns_as_text(connection, "journal_lines", ("debit", "credit"))


def migrate_currency_rates_to_text(connection):
    """Convert legacy SQLite currency exchange rates from binary REAL to decimal text."""
    if database_dialect(connection) != "sqlite":
        raise RuntimeError(
            "SQLite currency-rate migration cannot run on PostgreSQL; use NUMERIC columns."
        )
    if not table_column_info(connection, "currencies"):
        return False
    return _rebuild_real_columns_as_text(connection, "currencies", ("rate_to_base",))
