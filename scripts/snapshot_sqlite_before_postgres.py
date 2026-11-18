"""Create and verify an immutable SQLite snapshot before PostgreSQL cutover.

Uses SQLite's online backup API so committed WAL data is included consistently
without copying a live database file byte-for-byte. The source is opened
read-only and is never modified. Existing snapshots are never overwritten.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid
from urllib.parse import quote

from app_paths import BACKUP_DIR, DB_PATH


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = "file:" + quote(path.as_posix(), safe="/:") + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _check_database(connection: sqlite3.Connection) -> tuple[list[str], list[dict]]:
    integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    foreign_keys = [
        dict(row) for row in connection.execute("PRAGMA foreign_key_check").fetchall()
    ]
    if integrity != ["ok"]:
        raise ValueError("SQLite integrity_check failed: " + "; ".join(integrity[:10]))
    if foreign_keys:
        raise ValueError(
            "SQLite foreign_key_check failed; snapshot is not approved: "
            + json.dumps(foreign_keys[:20], ensure_ascii=False)
        )
    return integrity, foreign_keys


def _table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    names = [
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    counts = {}
    for name in names:
        quoted = '"' + name.replace('"', '""') + '"'
        counts[name] = int(connection.execute(
            f"SELECT COUNT(*) FROM {quoted}"
        ).fetchone()[0])
    return counts


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_sqlite_snapshot(
    source_path: str | Path = DB_PATH,
    backup_dir: str | Path = BACKUP_DIR,
    *,
    created_at: datetime | None = None,
) -> dict:
    """Create a new, verified snapshot and manifest without altering the source."""
    source = Path(source_path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError("SQLite source must be an existing database file.")

    destination_dir = Path(backup_dir).expanduser().resolve()
    destination_dir.mkdir(parents=True, exist_ok=True)
    moment = created_at or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    stamp = moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot = destination_dir / f"admrail-sqlite-{stamp}.sqlite3"
    manifest = destination_dir / f"admrail-sqlite-{stamp}.manifest.json"
    if snapshot.exists() or manifest.exists():
        raise FileExistsError("Snapshot destination already exists; refusing to overwrite it.")

    source_connection = _readonly_connection(source)
    target_connection = None
    created_snapshot = False
    try:
        _check_database(source_connection)
        source_counts = _table_counts(source_connection)

        target_connection = sqlite3.connect(str(snapshot), timeout=30)
        created_snapshot = True
        source_connection.backup(target_connection, pages=1000, sleep=0.05)
        _check_database(target_connection)
        snapshot_counts = _table_counts(target_connection)
        if source_counts != snapshot_counts:
            raise ValueError("SQLite snapshot table row counts differ from the source.")

        target_connection.close()
        target_connection = None
        checksum = _sha256(snapshot)
        payload = {
            "format_version": 1,
            "created_at_utc": moment.astimezone(timezone.utc).isoformat(),
            "source_file_name": source.name,
            "snapshot_file": snapshot.name,
            "snapshot_size_bytes": snapshot.stat().st_size,
            "sha256": checksum,
            "integrity_check": "ok",
            "foreign_key_violations": 0,
            "table_count": len(snapshot_counts),
            "row_count_total": sum(snapshot_counts.values()),
            "table_row_counts": snapshot_counts,
            "read_only_source": True,
            "snapshot_method": "sqlite_online_backup_api",
        }
        temporary_manifest = destination_dir / (
            f".{manifest.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temporary_manifest.open("x", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            # Exclusive destination check above plus replace only this newly
            # created temporary file; never replace an existing manifest.
            if manifest.exists():
                raise FileExistsError("Snapshot manifest already exists; refusing to overwrite it.")
            os.rename(temporary_manifest, manifest)
        finally:
            temporary_manifest.unlink(missing_ok=True)

        return {
            "snapshot": str(snapshot),
            "manifest": str(manifest),
            "sha256": checksum,
            "table_count": len(snapshot_counts),
            "row_count_total": sum(snapshot_counts.values()),
            "verified": True,
        }
    except Exception:
        if target_connection is not None:
            target_connection.close()
        if created_snapshot:
            snapshot.unlink(missing_ok=True)
        raise
    finally:
        source_connection.close()


def verify_sqlite_snapshot(
    snapshot_path: str | Path,
    manifest_path: str | Path | None = None,
) -> dict:
    """Verify checksum, SQLite integrity, foreign keys, and manifest row counts."""
    snapshot = Path(snapshot_path).expanduser().resolve(strict=True)
    manifest = (
        Path(manifest_path).expanduser().resolve(strict=True)
        if manifest_path
        else snapshot.with_name(snapshot.name.replace(".sqlite3", ".manifest.json"))
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    if payload.get("snapshot_file") != snapshot.name:
        raise ValueError("Snapshot manifest points to a different database file.")
    checksum = _sha256(snapshot)
    if checksum != payload.get("sha256"):
        raise ValueError("SQLite snapshot SHA-256 does not match its manifest.")
    connection = _readonly_connection(snapshot)
    try:
        _check_database(connection)
        counts = _table_counts(connection)
    finally:
        connection.close()
    if counts != payload.get("table_row_counts"):
        raise ValueError("SQLite snapshot table row counts do not match its manifest.")
    return {
        "verified": True,
        "snapshot": str(snapshot),
        "sha256": checksum,
        "table_count": len(counts),
        "row_count_total": sum(counts.values()),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(DB_PATH))
    parser.add_argument("--backup-dir", default=str(BACKUP_DIR))
    parser.add_argument("--verify", help="Verify an existing snapshot instead of creating one.")
    parser.add_argument("--manifest", help="Optional manifest path for --verify.")
    args = parser.parse_args(argv)
    try:
        if args.verify:
            result = verify_sqlite_snapshot(args.verify, args.manifest)
        else:
            result = create_sqlite_snapshot(args.source, args.backup_dir)
    except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
