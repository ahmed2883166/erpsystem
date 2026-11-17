"""Core NEXUS relational schema, separated from Flask startup orchestration.

This first extraction preserves the existing SQLite DDL byte-for-byte in
meaning. It intentionally does not claim PostgreSQL compatibility yet:
dialect-specific identity, trigger, and migration behavior still needs a
separate reviewed migration layer.
"""
from db_compat import execute_script, table_columns


CORE_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, full_name TEXT NOT NULL,
        password_hash TEXT NOT NULL, role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        must_change_password INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, name_ar TEXT NOT NULL,
        account_type TEXT NOT NULL, parent_id INTEGER REFERENCES accounts(id),
        is_group INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS journal_entries (
        id INTEGER PRIMARY KEY, entry_date TEXT NOT NULL, reference TEXT, description TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'posted', created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS journal_lines (
        id INTEGER PRIMARY KEY, entry_id INTEGER NOT NULL REFERENCES journal_entries(id) ON DELETE CASCADE,
        account_id INTEGER NOT NULL REFERENCES accounts(id), debit TEXT NOT NULL DEFAULT '0',
        credit TEXT NOT NULL DEFAULT '0', memo TEXT
    );
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id), action TEXT NOT NULL,
        details TEXT, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS company_settings (
        setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS currencies (
        code TEXT PRIMARY KEY, name_ar TEXT NOT NULL, symbol TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 0, is_base INTEGER NOT NULL DEFAULT 0,
        rate_to_base REAL NOT NULL DEFAULT 0, updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS tax_rates (
        id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, name_ar TEXT NOT NULL,
        rate REAL NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS login_attempts (
        id INTEGER PRIMARY KEY, username TEXT NOT NULL, remote_addr TEXT NOT NULL,
        attempted_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS accounting_periods (
        id INTEGER PRIMARY KEY, period_name TEXT NOT NULL, start_date TEXT NOT NULL,
        end_date TEXT NOT NULL, locked INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
    );
"""


def initialize_core_schema(connection):
    """Create the core tables idempotently on the supplied connection."""
    execute_script(connection, CORE_SCHEMA_SQL)
    return True


def ensure_legacy_core_columns(connection):
    """Add backward-compatible columns missing from older company databases.

    Existing values and primary keys are left untouched. The helper uses the
    shared schema-inspection adapter and is safe to run repeatedly.
    """
    additions = (
        ("users", "must_change_password", "INTEGER NOT NULL DEFAULT 0"),
        ("users", "permissions_json", "TEXT"),
        ("accounts", "is_group", "INTEGER NOT NULL DEFAULT 0"),
    )
    for table_name, column_name, declaration in additions:
        if column_name not in table_columns(connection, table_name):
            connection.execute(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {declaration}"
            )
    return True
