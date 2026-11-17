"""Isolated PostgreSQL connection prototype for the staged NEXUS migration.

This module is deliberately not wired into the Flask application. SQLite remains
the only runtime database until every SQL dialect and transaction behavior has
been migrated and verified.
"""
import os


DEFAULT_CONNECT_TIMEOUT_SECONDS = 5
MAX_CONNECT_TIMEOUT_SECONDS = 30
DEFAULT_APPLICATION_NAME = "NEXUS ERP"


def connect_postgres(
    dsn=None,
    *,
    autocommit=False,
    row_factory=None,
    connect_timeout=DEFAULT_CONNECT_TIMEOUT_SECONDS,
    application_name=DEFAULT_APPLICATION_NAME,
):
    """Open a PostgreSQL connection without changing the active SQLite path.

    The DSN is explicit or read from NEXUS_POSTGRES_DSN. There is intentionally
    no fallback to SQLite and no automatic database selection. By default,
    rows are dictionaries and PostgreSQL NUMERIC values remain Decimal objects.

    This is a connection prototype, not a drop-in replacement for sqlite3:
    callers must migrate SQL placeholders, row access, generated IDs, schema
    inspection, and transaction semantics before using it in application code.
    """
    dsn = str(dsn or os.environ.get("NEXUS_POSTGRES_DSN") or "").strip()
    if not dsn:
        raise RuntimeError(
            "PostgreSQL DSN is required; set NEXUS_POSTGRES_DSN or pass dsn explicitly."
        )
    if not isinstance(autocommit, bool):
        raise TypeError("autocommit must be a bool.")
    try:
        connect_timeout = int(connect_timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError("connect_timeout must be an integer from 1 to 30 seconds.") from exc
    if not 1 <= connect_timeout <= MAX_CONNECT_TIMEOUT_SECONDS:
        raise ValueError("connect_timeout must be between 1 and 30 seconds.")
    application_name = str(application_name or DEFAULT_APPLICATION_NAME).strip()
    if not application_name or len(application_name) > 63:
        raise ValueError("application_name must contain between 1 and 63 characters.")

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError(
            "PostgreSQL support is optional; install psycopg[binary]>=3.2,<4.0."
        ) from exc

    connection = psycopg.connect(
        dsn,
        autocommit=autocommit,
        row_factory=row_factory or dict_row,
        connect_timeout=connect_timeout,
        application_name=application_name,
    )
    return connection
