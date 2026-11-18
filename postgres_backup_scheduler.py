"""PostgreSQL-backed automatic backup scheduling.

This scheduler is deliberately separate from the SQLite scheduler until the
application runtime gate is lifted. It persists claims in company_settings and
uses a transaction-scoped advisory lock so multiple workers cannot run the
same scheduled backup concurrently.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import psycopg

from backup_scheduler import next_backup_datetime
from postgres_backup import create_postgres_backup_archive

_ADVISORY_LOCK_KEY = 1313162836
_scheduler_thread = None
_scheduler_lock = threading.Lock()
_scheduler_stop = threading.Event()


def _settings(connection):
    rows = connection.execute(
        """SELECT setting_key, setting_value
             FROM company_settings
            WHERE setting_key LIKE 'backup_%'
            FOR UPDATE"""
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def _write_settings(connection, values):
    connection.executemany(
        """INSERT INTO company_settings(setting_key, setting_value)
           VALUES (%s, %s)
           ON CONFLICT(setting_key) DO UPDATE
             SET setting_value=EXCLUDED.setting_value""",
        list(values.items()),
    )


def run_due_postgres_backup(dsn, backup_dir):
    """Claim and run one due PostgreSQL backup; return its archive or None."""
    with psycopg.connect(dsn) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK_KEY,))
        settings = _settings(connection)
        if settings.get("backup_enabled", "0") != "1":
            return None

        current = datetime.now()
        due_text = settings.get("backup_next_run", "")
        if not due_text:
            due = next_backup_datetime(
                settings.get("backup_frequency", "daily"),
                settings.get("backup_time", "23:00"),
                now=current,
                weekday=int(settings.get("backup_weekday", "0")),
                month_day=int(settings.get("backup_month_day", "1")),
                interval_days=int(settings.get("backup_interval_days", "1")),
            )
            _write_settings(connection, {
                "backup_next_run": due.isoformat(timespec="seconds"),
                "backup_last_status": "scheduled",
                "backup_last_error": "",
            })
            return None

        due = datetime.fromisoformat(due_text)
        if current < due:
            return None

        next_due = next_backup_datetime(
            settings.get("backup_frequency", "daily"),
            settings.get("backup_time", "23:00"),
            now=current,
            weekday=int(settings.get("backup_weekday", "0")),
            month_day=int(settings.get("backup_month_day", "1")),
            interval_days=int(settings.get("backup_interval_days", "1")),
        )
        _write_settings(connection, {
            "backup_next_run": next_due.isoformat(timespec="seconds"),
            "backup_last_status": "running",
            "backup_last_error": "",
        })
        keep_count = int(settings.get("backup_keep_count", "30"))

    try:
        archive = create_postgres_backup_archive(
            dsn, backup_dir, keep_count=keep_count
        )
        status, error, filename = "success", "", archive.name
    except Exception as exc:
        archive = None
        status, error, filename = "failed", str(exc)[:1000], ""
        retry_at = (datetime.now() + timedelta(minutes=5)).isoformat(timespec="seconds")
        with psycopg.connect(dsn) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK_KEY,))
            _write_settings(connection, {"backup_next_run": retry_at})

    with psycopg.connect(dsn) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK_KEY,))
        _write_settings(connection, {
            "backup_last_run": datetime.now().isoformat(timespec="seconds"),
            "backup_last_status": status,
            "backup_last_error": error,
            "backup_last_file": filename,
        })
    return archive


def start_postgres_backup_scheduler(dsn, backup_dir, poll_seconds=30):
    """Start one daemon scheduler per process; DB advisory lock coordinates workers."""
    global _scheduler_thread
    with _scheduler_lock:
        if _scheduler_thread and _scheduler_thread.is_alive():
            return _scheduler_thread
        _scheduler_stop.clear()

        def loop():
            while not _scheduler_stop.is_set():
                try:
                    run_due_postgres_backup(dsn, backup_dir)
                except Exception:
                    # Keep the daemon alive; persisted state is retried next cycle.
                    pass
                _scheduler_stop.wait(max(5, int(poll_seconds)))

        _scheduler_thread = threading.Thread(
            target=loop, name="nexus-postgres-backup-scheduler", daemon=True
        )
        _scheduler_thread.start()
        return _scheduler_thread


def stop_postgres_backup_scheduler(timeout=5):
    """Stop the local daemon cleanly, primarily for controlled shutdown and tests."""
    _scheduler_stop.set()
    thread = _scheduler_thread
    if thread and thread.is_alive():
        thread.join(timeout=max(0, float(timeout)))
    return not bool(thread and thread.is_alive())
