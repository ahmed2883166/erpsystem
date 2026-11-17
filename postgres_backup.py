"""Safe PostgreSQL logical backup and isolated-database restore helpers.

This module is separate from the SQLite scheduler/runtime. It uses PostgreSQL's
native custom archive format and never overwrites an existing database.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict

BACKUP_PREFIX = "nexus-postgres-backup-"
DUMP_MEMBER = "nexus.dump"
MANIFEST_MEMBER = "manifest.json"
MAX_MANIFEST_BYTES = 1024 * 1024
_DATABASE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


def _pg_environment(dsn, *, database_override=None):
    """Build libpq environment variables so credentials never appear in argv."""
    values = conninfo_to_dict(str(dsn or "").strip())
    if not values:
        raise ValueError("PostgreSQL connection information is required.")
    env = os.environ.copy()
    mapping = {
        "host": "PGHOST", "port": "PGPORT", "user": "PGUSER",
        "password": "PGPASSWORD", "sslmode": "PGSSLMODE",
        "sslrootcert": "PGSSLROOTCERT", "sslcert": "PGSSLCERT",
        "sslkey": "PGSSLKEY", "connect_timeout": "PGCONNECT_TIMEOUT",
        "application_name": "PGAPPNAME", "options": "PGOPTIONS",
    }
    for source, target in mapping.items():
        if values.get(source):
            env[target] = str(values[source])
    database = database_override or values.get("dbname")
    if database:
        env["PGDATABASE"] = str(database)
    return env, values


def _run(command, *, env, timeout):
    return subprocess.run(
        command, env=env, check=True, capture_output=True,
        text=True, timeout=timeout,
    )


def create_postgres_backup_archive(
    dsn, backup_dir, keep_count=30, *, pg_dump_path=None, pg_restore_path=None,
    timeout=3600,
):
    """Create a checksum-verified custom-format pg_dump archive."""
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    env, connection = _pg_environment(dsn)
    database_name = connection.get("dbname", "")
    if not database_name:
        raise ValueError("The PostgreSQL DSN must identify a database.")
    pg_dump = pg_dump_path or shutil.which("pg_dump")
    pg_restore = pg_restore_path or shutil.which("pg_restore")
    if not pg_dump or not pg_restore:
        raise RuntimeError("PostgreSQL client tools pg_dump and pg_restore are required.")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    archive_path = backup_dir / f"{BACKUP_PREFIX}{stamp}.zip"
    with tempfile.TemporaryDirectory(prefix="nexus-pg-backup-") as temporary:
        dump_path = Path(temporary) / DUMP_MEMBER
        _run([
            pg_dump, "--no-password", "--format=custom", "--no-owner",
            "--no-acl", "--file", str(dump_path),
        ], env=env, timeout=timeout)
        if not dump_path.is_file() or dump_path.stat().st_size <= 0:
            raise RuntimeError("pg_dump did not produce a valid non-empty archive.")
        hasher = hashlib.sha256()
        with dump_path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                hasher.update(chunk)
        manifest = {
            "application": "NEXUS ERP",
            "backup_format": "postgresql-custom",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "database_name": database_name,
            "dump_sha256": hasher.hexdigest(),
        }
        temporary_archive = archive_path.with_suffix(".zip.tmp")
        try:
            with zipfile.ZipFile(temporary_archive, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(dump_path, DUMP_MEMBER)
                archive.writestr(MANIFEST_MEMBER, json.dumps(manifest, ensure_ascii=False))
            validate_postgres_backup_archive(
                temporary_archive, pg_restore_path=pg_restore, timeout=timeout
            )
            temporary_archive.replace(archive_path)
        except Exception:
            temporary_archive.unlink(missing_ok=True)
            archive_path.unlink(missing_ok=True)
            raise

    archives = sorted(
        backup_dir.glob(f"{BACKUP_PREFIX}*.zip"),
        key=lambda item: item.stat().st_mtime, reverse=True,
    )
    keep_count = max(1, min(int(keep_count or 30), 365))
    for old in archives[keep_count:]:
        old.unlink(missing_ok=True)
    return archive_path


def validate_postgres_backup_archive(
    archive_path, *, pg_restore_path=None, timeout=300,
):
    """Validate NEXUS manifest, checksum and PostgreSQL custom archive TOC."""
    archive_path = Path(archive_path)
    pg_restore = pg_restore_path or shutil.which("pg_restore")
    if not pg_restore:
        raise RuntimeError("PostgreSQL client tool pg_restore is required.")
    with zipfile.ZipFile(archive_path, "r") as archive:
        names = archive.namelist()
        if (set(names) != {DUMP_MEMBER, MANIFEST_MEMBER}
                or names.count(DUMP_MEMBER) != 1
                or names.count(MANIFEST_MEMBER) != 1):
            raise ValueError("PostgreSQL backup must contain exactly nexus.dump and manifest.json.")
        if archive.getinfo(MANIFEST_MEMBER).file_size > MAX_MANIFEST_BYTES:
            raise ValueError("PostgreSQL backup manifest exceeds the allowed size.")
        try:
            manifest = json.loads(archive.read(MANIFEST_MEMBER).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("PostgreSQL backup manifest is invalid.") from exc
        if (not isinstance(manifest, dict)
                or manifest.get("application") != "NEXUS ERP"
                or manifest.get("backup_format") != "postgresql-custom"):
            raise ValueError("The archive is not a supported NEXUS PostgreSQL backup.")
        with tempfile.NamedTemporaryFile(prefix="nexus-pg-validate-", suffix=".dump") as dump:
            hasher = hashlib.sha256()
            total = 0
            with archive.open(DUMP_MEMBER, "r") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    total += len(chunk)
                    hasher.update(chunk)
                    dump.write(chunk)
            if not total or hasher.hexdigest() != manifest.get("dump_sha256"):
                raise ValueError("PostgreSQL backup checksum verification failed.")
            dump.flush()
            _run([pg_restore, "--list", dump.name], env=os.environ.copy(), timeout=timeout)
    return manifest


def restore_postgres_backup_to_new_database(
    archive_path, admin_dsn, *, database_name=None, pg_restore_path=None,
    timeout=3600,
):
    """Restore into a newly-created database; refuse to overwrite any existing DB."""
    manifest = validate_postgres_backup_archive(
        archive_path, pg_restore_path=pg_restore_path, timeout=min(timeout, 300)
    )
    pg_restore = pg_restore_path or shutil.which("pg_restore")
    if not pg_restore:
        raise RuntimeError("PostgreSQL client tool pg_restore is required.")
    target_name = database_name or ("nexus_restore_" + uuid.uuid4().hex[:16])
    if not _DATABASE_NAME.fullmatch(str(target_name)):
        raise ValueError("Restore database name must be lowercase letters, digits and underscores.")
    env, _ = _pg_environment(admin_dsn)
    try:
        import psycopg
        from psycopg import sql
    except ImportError as exc:
        raise RuntimeError("PostgreSQL restore requires psycopg 3.") from exc

    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        exists = admin.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (target_name,)
        ).fetchone()
        if exists:
            raise FileExistsError("Restore target already exists; no existing database was changed.")
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target_name)))

    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            with tempfile.NamedTemporaryFile(prefix="nexus-pg-restore-", suffix=".dump") as dump:
                with archive.open(DUMP_MEMBER, "r") as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        dump.write(chunk)
                dump.flush()
                restore_env = dict(env)
                restore_env["PGDATABASE"] = target_name
                _run([
                    pg_restore, "--no-password", "--exit-on-error",
                    "--single-transaction", "--no-owner", "--no-acl",
                    "--dbname", target_name, dump.name,
                ], env=restore_env, timeout=timeout)
    except Exception:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(target_name)))
        raise
    return {
        "database_name": target_name,
        "source_database_name": manifest.get("database_name", ""),
        "restored": True,
    }
