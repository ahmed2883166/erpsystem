"""SQLite audit-log compatibility migration.

The append-only triggers use SQLite syntax. PostgreSQL needs its own reviewed
trigger function and migration, so this helper fails closed on other dialects.
"""
from db_compat import database_dialect, table_columns, execute_script


_AUDIT_COLUMNS = (
    ("entity_type", "TEXT"),
    ("entity_id", "TEXT"),
    ("remote_addr", "TEXT"),
    ("outcome", "TEXT NOT NULL DEFAULT 'success'"),
    ("actor_name", "TEXT"),
    ("before_values", "TEXT"),
    ("after_values", "TEXT"),
    ("submitted_values", "TEXT"),
    ("reason", "TEXT"),
    ("request_id", "TEXT"),
)


def initialize_sqlite_audit_schema(connection):
    """Add audit metadata and append-only protections without replacing rows."""
    if database_dialect(connection) != "sqlite":
        raise RuntimeError(
            "SQLite audit migration cannot run on PostgreSQL; use the PostgreSQL audit migration."
        )
    columns = table_columns(connection, "audit_log")
    for column, declaration in _AUDIT_COLUMNS:
        if column not in columns:
            connection.execute(
                f"ALTER TABLE audit_log ADD COLUMN {column} {declaration}"
            )
    for statement in (
        "CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log(created_at,id)",
        "CREATE INDEX IF NOT EXISTS idx_audit_log_user_action ON audit_log(user_id,action,created_at)",
        "CREATE INDEX IF NOT EXISTS idx_audit_log_request_id ON audit_log(request_id)",
        "CREATE INDEX IF NOT EXISTS idx_audit_log_entity_created ON audit_log(entity_type,created_at,id)",
        "CREATE INDEX IF NOT EXISTS idx_audit_log_outcome_created ON audit_log(outcome,created_at,id)",
    ):
        connection.execute(statement)
    execute_script(connection, """
        CREATE TRIGGER IF NOT EXISTS audit_log_block_update
        BEFORE UPDATE ON audit_log
        BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS audit_log_block_delete
        BEFORE DELETE ON audit_log
        BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
    """)
    return True
