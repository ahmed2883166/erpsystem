"""Shared SQLite connection policy for NEXUS ERP.

Every application connection gets the same lock timeout, foreign-key enforcement,
row mapping and exact Decimal SQL aggregates. WAL is enabled once during startup,
not on every request connection.
"""
import sqlite3
from pathlib import Path
from flask import g, has_app_context
from report_request_control import current_report_deadline, make_sqlite_deadline_callback
from db_sql import execute_compat
from sqlite_maintenance import enable_sqlite_wal


DEFAULT_TIMEOUT_SECONDS = 20


def connect_database(path, *, timeout=DEFAULT_TIMEOUT_SECONDS, isolation_level="", decimal_aggregates=True):
    """Open a consistently configured SQLite connection.

    Keep transaction behavior explicit at call sites: the main app uses SQLite's
    default deferred transactions (empty string), while modules that historically
    used autocommit continue to pass isolation_level=None.
    """
    # Reuse one connection per database/options tuple during a Flask app
    # context. This prevents route helpers across modules from leaking multiple
    # handles to the same SQLite file and makes Windows file cleanup reliable.
    cache_key = (
        str(Path(path).resolve()), float(timeout), isolation_level, bool(decimal_aggregates)
    )
    if has_app_context():
        connections = getattr(g, "_nexus_database_connections", None)
        if connections is None:
            connections = {}
            g._nexus_database_connections = connections
        cached = connections.get(cache_key)
        if cached is not None:
            return cached

    connection = sqlite3.connect(
        str(Path(path)), timeout=timeout, isolation_level=isolation_level
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
    if has_app_context():
        connections[cache_key] = connection
    return connection


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
