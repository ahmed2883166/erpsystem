"""Safely bootstrap a brand-new NEXUS PostgreSQL company database.

This command installs the reviewed schema baseline and runs the application's
idempotent schema/data bootstrap. It does not enable PostgreSQL runtime mode:
the separate runtime compatibility gate remains authoritative until all
operational modules have passed end-to-end verification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

import psycopg
from psycopg.conninfo import make_conninfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from postgres_baseline import install_postgres_baseline
from postgres_migrations import quote_schema_name


def _install_or_verify_baseline(dsn: str, schema: str, ddl_path: Path) -> str:
    ddl = ddl_path.read_text(encoding="utf-8")
    expected_digest = hashlib.sha256(ddl.encode("utf-8")).hexdigest()
    quoted_schema = quote_schema_name(schema)

    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(f"CREATE SCHEMA IF NOT EXISTS {quoted_schema}")
        connection.execute(f"SET search_path TO {quoted_schema}")
        installed = connection.execute(
            "SELECT baseline_sha256 FROM nexus_schema_installation WHERE id=1"
        ).fetchone() if _table_exists(connection, "nexus_schema_installation") else None

        if installed:
            recorded_digest = str(installed[0]).strip()
            if recorded_digest != expected_digest:
                raise RuntimeError(
                    "The target schema already has a different NEXUS baseline. "
                    "Refusing to overwrite or mix schema versions."
                )
            return recorded_digest

        return install_postgres_baseline(connection, ddl, schema=schema)


def _table_exists(connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_schema=current_schema() AND table_name=%s "
        "AND table_type='BASE TABLE')",
        (table_name,),
    ).fetchone()
    return bool(row[0])


def _verify_bootstrap(connection) -> dict:
    required_tables = {
        "users", "accounts", "company_settings", "journal_entries",
        "journal_lines", "customers", "suppliers", "stock_products",
        "stock_warehouses", "sales_invoices", "supplier_bills",
        "bank_transactions", "fixed_assets", "employees", "payroll_runs",
    }
    existing = {
        row[0] for row in connection.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=current_schema() AND table_type='BASE TABLE'"
        ).fetchall()
    }
    missing = sorted(required_tables - existing)
    if missing:
        raise RuntimeError(
            "PostgreSQL bootstrap is missing required application tables: "
            + ", ".join(missing)
        )

    counts = {}
    for table in ("users", "accounts", "company_settings"):
        # Names come from the fixed allow-list above, not user input.
        counts[table] = int(connection.execute(
            f'SELECT COUNT(*) FROM "{table}"'
        ).fetchone()[0])
    if any(value < 1 for value in counts.values()):
        raise RuntimeError(
            "Bootstrap did not seed the administrator, chart of accounts, "
            f"and company settings: {counts}"
        )

    return {
        "required_tables": len(required_tables),
        "verified_tables": sorted(required_tables),
        "seed_counts": counts,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ddl", required=True, type=Path,
                        help="Reviewed generated nexus_full_schema.sql file.")
    parser.add_argument("--dsn", default=os.environ.get("NEXUS_POSTGRES_DSN", ""),
                        help="Target PostgreSQL DSN; defaults to NEXUS_POSTGRES_DSN.")
    parser.add_argument("--schema", default="nexus",
                        help="Dedicated empty schema for this company (default: nexus).")
    parser.add_argument("--data-dir", type=Path, required=True,
                        help="Directory for one-time admin credential and application logs.")
    parser.add_argument("--backup-dir", type=Path, required=True,
                        help="Directory reserved for PostgreSQL backup archives.")
    args = parser.parse_args(argv)

    dsn = args.dsn.strip()
    if not dsn:
        parser.error("Provide --dsn or set NEXUS_POSTGRES_DSN.")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", args.schema):
        parser.error("--schema must be a lowercase PostgreSQL identifier.")
    ddl_path = args.ddl.expanduser().resolve()
    if not ddl_path.is_file():
        parser.error(f"Baseline SQL file not found: {ddl_path}")

    data_dir = args.data_dir.expanduser().resolve()
    backup_dir = args.backup_dir.expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=True)

    baseline_sha256 = _install_or_verify_baseline(dsn, args.schema, ddl_path)
    scoped_dsn = make_conninfo(dsn, options=f"-csearch_path={args.schema}")

    # Importing app must not trigger an implicit second initialization.
    os.environ["NEXUS_DB_BACKEND"] = "postgresql"
    os.environ["NEXUS_POSTGRES_DSN"] = scoped_dsn
    os.environ["NEXUS_DATA_DIR"] = str(data_dir)
    os.environ["NEXUS_BACKUP_DIR"] = str(backup_dir)
    os.environ["NEXUS_SKIP_DB_INIT"] = "1"
    os.environ.setdefault("NEXUS_DEMO_MODE", "0")

    import app as nexus_app
    import database as database_module

    # The bootstrap bypass is process-local and scoped to this explicit
    # fresh-install command. app.db() and connect_database() each hold their
    # own imported reference to the runtime gate, so both references must be
    # patched together. The canonical database_backend policy stays fail-closed.
    original_app_gate = nexus_app.require_supported_runtime_backend
    original_connection_gate = database_module.require_supported_runtime_backend
    bootstrap_gate = lambda: "postgresql"
    nexus_app.require_supported_runtime_backend = bootstrap_gate
    database_module.require_supported_runtime_backend = bootstrap_gate
    try:
        nexus_app.initialize_database()
        with nexus_app.app.app_context():
            connection = nexus_app.db()
            try:
                verification = _verify_bootstrap(connection)
                connection.commit()
            finally:
                connection.close()
    finally:
        nexus_app.require_supported_runtime_backend = original_app_gate
        database_module.require_supported_runtime_backend = original_connection_gate

    print(json.dumps({
        "status": "initialized",
        "backend": "postgresql",
        "schema": args.schema,
        "baseline_sha256": baseline_sha256,
        "verification": verification,
        "runtime_enabled": False,
        "runtime_note": (
            "PostgreSQL runtime remains fail-closed until all ERP workflows "
            "pass the separate compatibility gate."
        ),
        "admin_credential_file": str(data_dir / ".nexus_initial_admin_password"),
        "backup_directory": str(backup_dir),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
