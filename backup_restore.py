"""Safe validation and rollback helpers for restoring NEXUS SQLite backups."""
import json
import sqlite3
import zipfile
from pathlib import Path

MAX_RESTORE_DATABASE_BYTES = 2 * 1024 * 1024 * 1024
REQUIRED_TABLES = {"users", "accounts", "journal_entries", "journal_lines"}


def extract_validated_backup(archive_path, candidate_path,
                             max_database_bytes=MAX_RESTORE_DATABASE_BYTES):
    """Extract only nexus.db and validate its identity, size, schema and integrity."""
    archive_path, candidate_path = Path(archive_path), Path(candidate_path)
    with zipfile.ZipFile(archive_path, "r") as archive:
        database_members = [item for item in archive.infolist() if item.filename == "nexus.db"]
        if len(database_members) != 1:
            raise ValueError("يجب أن تحتوي النسخة على ملف قاعدة بيانات واحد باسم nexus.db.")
        member = database_members[0]
        if member.file_size <= 0 or member.file_size > max_database_bytes:
            raise ValueError("حجم قاعدة البيانات داخل النسخة غير مسموح به.")
        manifests = [item for item in archive.infolist() if item.filename == "manifest.json"]
        if len(manifests) > 1:
            raise ValueError("ملف النسخة يحتوي على أكثر من Manifest.")
        if manifests:
            try:
                manifest = json.loads(archive.read(manifests[0]).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("ملف Manifest غير صالح.") from exc
            if manifest.get("application") not in (None, "NEXUS ERP"):
                raise ValueError("النسخة لا تخص نظام NEXUS ERP.")
        written = 0
        with archive.open(member, "r") as source, candidate_path.open("wb") as target:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_database_bytes:
                    raise ValueError("حجم قاعدة البيانات المستخرجة تجاوز الحد المسموح.")
                target.write(chunk)
    connection = sqlite3.connect(str(candidate_path))
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if integrity != "ok" or not REQUIRED_TABLES.issubset(tables):
            raise ValueError("قاعدة البيانات غير سليمة أو لا تطابق هيكل NEXUS ERP.")
    finally:
        connection.close()
    return candidate_path


def create_database_safety_copy(database_path, safety_path):
    """Create and verify a consistent SQLite snapshot before restoration."""
    source = sqlite3.connect(str(database_path), timeout=30)
    destination = sqlite3.connect(str(safety_path), timeout=30)
    try:
        source.backup(destination)
        integrity = destination.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError("تعذر إنشاء نسخة أمان سليمة من قاعدة البيانات الحالية.")
    finally:
        destination.close()
        source.close()
    return Path(safety_path)


def restore_database_with_rollback(candidate_path, database_path, safety_path,
                                   initialize_callback):
    """Restore into SQLite and roll back to the safety snapshot if initialization fails."""
    candidate = sqlite3.connect(str(candidate_path), timeout=60)
    destination = sqlite3.connect(str(database_path), timeout=60)
    try:
        candidate.backup(destination, pages=256, sleep=0.05)
        destination.commit()
    finally:
        destination.close()
        candidate.close()
    try:
        initialize_callback()
    except Exception as restore_error:
        safety = sqlite3.connect(str(safety_path), timeout=60)
        destination = sqlite3.connect(str(database_path), timeout=60)
        try:
            safety.backup(destination, pages=256, sleep=0.05)
            destination.commit()
        finally:
            destination.close()
            safety.close()
        raise RuntimeError(
            "فشل تهيئة قاعدة البيانات بعد الاسترجاع؛ تمت إعادة قاعدة البيانات السابقة من نسخة الأمان."
        ) from restore_error
    return True
