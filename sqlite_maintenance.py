"""SQLite-only maintenance primitives kept outside the application connection layer.

These helpers deliberately do not select a runtime backend. They isolate WAL,
online snapshots, integrity checks, and SQLite schema inspection so a future
PostgreSQL adapter cannot accidentally inherit SQLite maintenance behavior.
"""
import sqlite3
from pathlib import Path


DEFAULT_MAINTENANCE_TIMEOUT = 20


def connect_sqlite_maintenance(path, *, timeout=DEFAULT_MAINTENANCE_TIMEOUT):
    """Open a short-lived SQLite connection for maintenance operations."""
    connection = sqlite3.connect(str(Path(path)), timeout=timeout, isolation_level=None)
    connection.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
    return connection


def enable_sqlite_wal(path, *, timeout=DEFAULT_MAINTENANCE_TIMEOUT):
    """Enable persistent SQLite WAL mode during startup."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = connect_sqlite_maintenance(target, timeout=timeout)
    try:
        mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise RuntimeError(f"Could not enable SQLite WAL mode (reported {mode!r}).")
        return str(mode).lower()
    finally:
        connection.close()


def sqlite_integrity_check(connection):
    """Return SQLite's integrity_check result without hiding database errors."""
    return str(connection.execute("PRAGMA integrity_check").fetchone()[0])


def sqlite_table_names(connection):
    """Return user table names from a SQLite database."""
    return {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def online_backup_sqlite(source_path, destination_path, *, timeout=20):
    """Create a consistent SQLite online snapshot and verify its integrity."""
    source_path, destination_path = Path(source_path), Path(destination_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    source = connect_sqlite_maintenance(source_path, timeout=timeout)
    destination = connect_sqlite_maintenance(destination_path, timeout=timeout)
    try:
        source.backup(destination)
        integrity = sqlite_integrity_check(destination)
        if integrity != "ok":
            raise RuntimeError("SQLite online backup failed its integrity check.")
    finally:
        destination.close()
        source.close()
    return destination_path


def restore_sqlite_snapshot(source_path, destination_path, *, timeout=60):
    """Replace a SQLite database from a verified snapshot using SQLite backup API."""
    source = connect_sqlite_maintenance(source_path, timeout=timeout)
    destination = connect_sqlite_maintenance(destination_path, timeout=timeout)
    try:
        source.backup(destination, pages=256, sleep=0.05)
        integrity = sqlite_integrity_check(destination)
        if integrity != "ok":
            raise RuntimeError("Restored SQLite database failed its integrity check.")
    finally:
        destination.close()
        source.close()
    return True
