"""Reliable local automatic backups for NEXUS ERP.

A lightweight daemon checks the persisted schedule. A SQLite write transaction
claims each due run so multiple Waitress/Gunicorn workers cannot create duplicates.
"""
import json
import os
import sqlite3
import threading
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

_scheduler_thread = None
_scheduler_lock = threading.Lock()
BACKUP_PREFIX = "nexus-backup-"


def _parse_time(value):
    try:
        hour, minute = (int(part) for part in str(value).split(":", 1))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except (TypeError, ValueError):
        pass
    raise ValueError("وقت النسخ الاحتياطي غير صحيح.")


def next_backup_datetime(frequency, backup_time, now=None, weekday=0, month_day=1, interval_days=1):
    now = now or datetime.now()
    hour, minute = _parse_time(backup_time)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if frequency == "daily":
        return candidate if candidate > now else candidate + timedelta(days=1)
    if frequency == "weekly":
        weekday = int(weekday)
        if weekday not in range(7):
            raise ValueError("يوم النسخ الأسبوعي غير صحيح.")
        candidate += timedelta(days=(weekday - candidate.weekday()) % 7)
        return candidate if candidate > now else candidate + timedelta(days=7)
    if frequency == "monthly":
        import calendar
        month_day = int(month_day)
        if month_day not in range(1, 32):
            raise ValueError("يوم النسخ الشهري يجب أن يكون من 1 إلى 31.")
        year, month = now.year, now.month
        for _ in range(24):
            day = min(month_day, calendar.monthrange(year, month)[1])
            candidate = datetime(year, month, day, hour, minute)
            if candidate > now:
                return candidate
            month += 1
            if month == 13:
                year, month = year + 1, 1
        raise ValueError("تعذر حساب موعد النسخة الشهرية.")
    if frequency == "every_n_days":
        interval_days = int(interval_days)
        if interval_days < 1 or interval_days > 365:
            raise ValueError("الفاصل بين النسخ يجب أن يكون من يوم إلى 365 يومًا.")
        return candidate if candidate > now else candidate + timedelta(days=interval_days)
    raise ValueError("تكرار النسخ الاحتياطي غير مدعوم.")


def _connect(db_path):
    connection = sqlite3.connect(str(db_path), timeout=20, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=20000")
    return connection


def create_backup_archive(db_path, backup_dir, keep_count=30):
    """Create a consistent SQLite online backup, zip it, and prune old archives."""
    db_path, backup_dir = Path(db_path), Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_path = backup_dir / f"{BACKUP_PREFIX}{stamp}.zip"
    snapshot_path = backup_dir / f".snapshot-{stamp}-{os.getpid()}.db"
    source = _connect(db_path)
    destination = sqlite3.connect(str(snapshot_path), timeout=20)
    try:
        source.backup(destination)
        integrity = destination.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError("فشل فحص سلامة قاعدة البيانات في النسخة الاحتياطية.")
    finally:
        destination.close()
        source.close()
    try:
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(snapshot_path, "nexus.db")
            archive.writestr("manifest.json", json.dumps({
                "application": "NEXUS ERP",
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "database_integrity": "ok",
            }, ensure_ascii=False))
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise
    finally:
        snapshot_path.unlink(missing_ok=True)
    archives = sorted(backup_dir.glob(f"{BACKUP_PREFIX}*.zip"), key=lambda item: item.stat().st_mtime, reverse=True)
    keep_count = max(1, min(int(keep_count or 30), 365))
    for old in archives[keep_count:]:
        old.unlink(missing_ok=True)
    return archive_path


def _settings(connection):
    return {row["setting_key"]: row["setting_value"] for row in connection.execute(
        "SELECT setting_key,setting_value FROM company_settings WHERE setting_key LIKE 'backup_%'"
    )}


def run_due_backup(db_path, backup_dir):
    """Claim and execute one due backup. Returns its path or None when not due."""
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        settings = _settings(connection)
        if settings.get("backup_enabled", "0") != "1":
            connection.rollback()
            return None
        due_text = settings.get("backup_next_run", "")
        if not due_text:
            due = next_backup_datetime(
                settings.get("backup_frequency", "daily"),
                settings.get("backup_time", "23:00"),
                weekday=int(settings.get("backup_weekday", "0")),
                month_day=int(settings.get("backup_month_day", "1")),
                interval_days=int(settings.get("backup_interval_days", "1")),
            )
            connection.execute("UPDATE company_settings SET setting_value=? WHERE setting_key='backup_next_run'",
                               (due.isoformat(timespec="seconds"),))
            connection.commit()
            return None
        due = datetime.fromisoformat(due_text)
        current = datetime.now()
        if current < due:
            connection.rollback()
            return None
        next_due = next_backup_datetime(
            settings.get("backup_frequency", "daily"),
            settings.get("backup_time", "23:00"),
            now=current,
            weekday=int(settings.get("backup_weekday", "0")),
            month_day=int(settings.get("backup_month_day", "1")),
            interval_days=int(settings.get("backup_interval_days", "1")),
        )
        connection.execute("""UPDATE company_settings SET setting_value=CASE setting_key
            WHEN 'backup_next_run' THEN ? WHEN 'backup_last_status' THEN 'running'
            WHEN 'backup_last_error' THEN '' ELSE setting_value END
            WHERE setting_key IN ('backup_next_run','backup_last_status','backup_last_error')""",
            (next_due.isoformat(timespec="seconds"),))
        connection.commit()
        keep = int(settings.get("backup_keep_count", "30"))
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    try:
        archive = create_backup_archive(db_path, backup_dir, keep)
        status, error = "success", ""
        filename = archive.name
    except Exception as exc:
        status, error, filename = "failed", str(exc)[:1000], ""
        archive = None
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        values = {
            "backup_last_run": datetime.now().isoformat(timespec="seconds"),
            "backup_last_status": status,
            "backup_last_error": error,
            "backup_last_file": filename,
        }
        if status == "failed":
            values["backup_next_run"] = (datetime.now() + timedelta(minutes=5)).isoformat(timespec="seconds")
        connection.executemany("""INSERT INTO company_settings(setting_key,setting_value)
            VALUES(?,?) ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value""",
            list(values.items()))
        connection.commit()
    finally:
        connection.close()
    return archive


def start_backup_scheduler(db_path, backup_dir, poll_seconds=30):
    """Start one daemon per process; database claiming prevents duplicate runs across workers."""
    global _scheduler_thread
    with _scheduler_lock:
        if _scheduler_thread and _scheduler_thread.is_alive():
            return _scheduler_thread

        def loop():
            while True:
                try:
                    run_due_backup(db_path, backup_dir)
                except Exception:
                    # Keep the scheduler alive; the next polling cycle retries after
                    # the persisted due time has been claimed/advanced.
                    pass
                time.sleep(max(5, int(poll_seconds)))

        _scheduler_thread = threading.Thread(target=loop, name="nexus-backup-scheduler", daemon=True)
        _scheduler_thread.start()
        return _scheduler_thread
