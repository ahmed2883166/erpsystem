"""Shared SQLite/PostgreSQL connection factory for NEXUS ERP.

SQLite remains the default runtime. PostgreSQL connections are opened only when
NEXUS_DB_BACKEND=postgresql is explicitly selected; application startup still
has a separate fail-closed migration gate until every module is compatible.
"""
import sqlite3
import time
import weakref
from pathlib import Path
from flask import g, has_app_context
from report_request_control import current_report_deadline, make_sqlite_deadline_callback
from db_sql import execute_compat
from sqlite_maintenance import enable_sqlite_wal
from database_backend import configured_database_backend


DEFAULT_TIMEOUT_SECONDS = 20


class _TrackedSQLiteConnection(sqlite3.Connection):
    """SQLite connection that can be weakly tracked without extending its lifetime."""
    __slots__ = ("__weakref__",)


def _close_app_context_connections(connections):
    """Close live SQLite or PostgreSQL connections at app-context teardown."""
    for reference in tuple(connections):
        connection = reference() if isinstance(reference, weakref.ReferenceType) else reference
        if connection is not None:
            try:
                connection.close()
            except Exception:
                # Teardown must continue closing the remaining retained handles.
                pass
    connections.clear()


def _teardown_database_connections(exception=None):
    """Close retained database connections when Flask pops its app context."""
    connections = getattr(g, "_nexus_database_connections", None)
    if connections is not None:
        _close_app_context_connections(connections)
    strong_connections = getattr(g, "_nexus_strong_database_connections", None)
    if strong_connections is not None:
        _close_app_context_connections(strong_connections)


def register_database_connection_teardown(app):
    """Register generic DB cleanup during app setup, before its first request."""
    if not app.extensions.get("nexus_database_connection_teardown"):
        app.teardown_appcontext(_teardown_database_connections)
        app.extensions["nexus_database_connection_teardown"] = True


def register_sqlite_connection_teardown(app):
    """Backward-compatible alias for the generic connection teardown hook."""
    register_database_connection_teardown(app)


def _track_connection(connection):
    if has_app_context():
        connections = getattr(g, "_nexus_database_connections", None)
        if connections is None:
            connections = set()
            g._nexus_database_connections = connections
        try:
            connections.add(weakref.ref(connection, connections.discard))
        except TypeError:
            # Some C-extension DB-API connections do not expose weak references.
            # Retain only those handles until app-context teardown.
            strong_connections = getattr(g, "_nexus_strong_database_connections", None)
            if strong_connections is None:
                strong_connections = []
                g._nexus_strong_database_connections = strong_connections
            strong_connections.append(connection)
    return connection


def connect_database(path, *, timeout=DEFAULT_TIMEOUT_SECONDS, isolation_level="", decimal_aggregates=True):
    """Open the explicitly selected database using the shared DB-API contract.

    The path argument is used only by SQLite. PostgreSQL requires
    NEXUS_POSTGRES_DSN and never falls back to the SQLite path. SQLite's
    isolation_level=None maps to PostgreSQL autocommit=True; all other values
    retain explicit transaction mode (autocommit=False).
    """
    if configured_database_backend() == "postgresql":
        from postgres_database import connect_postgres

        connection = connect_postgres(
            autocommit=isolation_level is None,
            connect_timeout=max(1, min(int(timeout), 30)),
        )
        deadline = current_report_deadline()
        if deadline is not None:
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            original_autocommit = connection.autocommit
            try:
                if not original_autocommit:
                    connection.autocommit = True
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, false)",
                    (f"{remaining_ms}ms",),
                )
            finally:
                if not original_autocommit:
                    connection.autocommit = False
        return _track_connection(connection)
    # Track weak references only. A strong list here changes historical behavior:
    # it keeps otherwise-unreferenced connections alive until request teardown.
    # Weak tracking preserves prompt refcount/GC closure while still allowing
    # teardown to close a connection that a caller deliberately retained.
    connection = sqlite3.connect(
        str(Path(path)), timeout=timeout, isolation_level=isolation_level,
        factory=_TrackedSQLiteConnection,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
    if decimal_aggregates:
        # Local import avoids a module cycle: accounting_core does not depend on
        # the connection factory.
        from accounting_core import register_decimal_aggregates
        register_decimal_aggregates(connection)
    deadline = current_report_deadline()
    if deadline is not None:
        # Covers connections opened inside blueprints as well as app.db().
        connection.set_progress_handler(make_sqlite_deadline_callback(deadline), 10000)
    return _track_connection(connection)


def enable_wal(path, *, timeout=DEFAULT_TIMEOUT_SECONDS):
    """Backward-compatible wrapper for the isolated SQLite maintenance layer."""
    return enable_sqlite_wal(path, timeout=timeout)


def _first_column_value(row):
    """Read the first selected value from SQLite rows, tuples, or mapping rows."""
    if row is None:
        return None
    keys = row.keys() if hasattr(row, "keys") else None
    if keys:
        return row[next(iter(keys))]
    return row[0]


def fetch_page(connection, query, count_query, params=(), count_params=None, page=1, per_page=50):
    """Execute a bounded, stable SQL page and return pagination metadata."""
    try:
        page = max(1, int(page or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = int(per_page or 50)
    except (TypeError, ValueError):
        per_page = 50
    if per_page not in (50, 100):
        per_page = 50
    count_params = params if count_params is None else count_params
    count_row = execute_compat(connection, count_query, count_params).fetchone()
    total = int(_first_column_value(count_row) or 0)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    rows = execute_compat(
        connection,
        query.rstrip().rstrip(";") + " LIMIT ? OFFSET ?",
        tuple(params) + (per_page, (page - 1) * per_page),
    ).fetchall()
    return {"rows": rows, "total": total, "page": page, "pages": pages, "per_page": per_page}
