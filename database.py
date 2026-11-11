"""Shared SQLite connection policy for NEXUS ERP.

Every application connection gets the same lock timeout, foreign-key enforcement,
row mapping and exact Decimal SQL aggregates. WAL is enabled once during startup,
not on every request connection.
"""
import sqlite3
from pathlib import Path


DEFAULT_TIMEOUT_SECONDS = 20


def connect_database(path, *, timeout=DEFAULT_TIMEOUT_SECONDS, isolation_level="", decimal_aggregates=True):
    """Open a consistently configured SQLite connection.

    Keep transaction behavior explicit at call sites: the main app uses SQLite's
    default deferred transactions (empty string), while modules that historically
    used autocommit continue to pass isolation_level=None.
    """
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
    return connection


def enable_wal(path, *, timeout=DEFAULT_TIMEOUT_SECONDS):
    """Enable persistent WAL mode once during application initialization."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(target), timeout=timeout)
    try:
        connection.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
        mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise RuntimeError(f"Could not enable SQLite WAL mode (reported {mode!r}).")
        return str(mode).lower()
    finally:
        connection.close()
