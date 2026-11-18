"""PostgreSQL audit-log schema and append-only protections."""
from db_compat import database_dialect, table_columns
from db_sql import execute_compat


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


def initialize_postgres_audit_schema(connection):
    """Idempotently add audit metadata and immutable-log triggers."""
    if database_dialect(connection) != "postgresql":
        raise RuntimeError("PostgreSQL audit migration requires a PostgreSQL connection.")
    columns = table_columns(connection, "audit_log")
    for column, declaration in _AUDIT_COLUMNS:
        if column not in columns:
            execute_compat(
                connection,
                f"ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS {column} {declaration}",
            )
    indexes = (
        "CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log(created_at,id)",
        "CREATE INDEX IF NOT EXISTS idx_audit_log_user_action ON audit_log(user_id,action,created_at)",
        "CREATE INDEX IF NOT EXISTS idx_audit_log_request_id ON audit_log(request_id)",
        "CREATE INDEX IF NOT EXISTS idx_audit_log_entity_created ON audit_log(entity_type,created_at,id)",
        "CREATE INDEX IF NOT EXISTS idx_audit_log_outcome_created ON audit_log(outcome,created_at,id)",
    )
    for statement in indexes:
        execute_compat(connection, statement)
    execute_compat(connection, """
        CREATE OR REPLACE FUNCTION nexus_reject_audit_log_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $nexus_audit$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only';
        END;
        $nexus_audit$
    """)
    execute_compat(connection, """
        DROP TRIGGER IF EXISTS audit_log_block_update ON audit_log
    """)
    execute_compat(connection, """
        DROP TRIGGER IF EXISTS audit_log_block_delete ON audit_log
    """)
    execute_compat(connection, """
        CREATE TRIGGER audit_log_block_update
        BEFORE UPDATE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION nexus_reject_audit_log_mutation()
    """)
    execute_compat(connection, """
        CREATE TRIGGER audit_log_block_delete
        BEFORE DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION nexus_reject_audit_log_mutation()
    """)
    return True
