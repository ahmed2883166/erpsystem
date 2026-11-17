"""Install a generated NEXUS PostgreSQL baseline into a fresh target schema."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from postgres_baseline import install_postgres_baseline_file
from postgres_database import connect_postgres


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ddl", required=True, help="Path to nexus_full_schema.sql")
    parser.add_argument(
        "--dsn",
        default=os.environ.get("NEXUS_POSTGRES_DSN", ""),
        help="Target PostgreSQL DSN (defaults to NEXUS_POSTGRES_DSN).",
    )
    parser.add_argument("--schema", default="nexus", help="New, empty target schema name.")
    args = parser.parse_args(argv)

    if not args.dsn.strip():
        parser.error("Provide --dsn or set NEXUS_POSTGRES_DSN.")
    ddl_path = Path(args.ddl).expanduser().resolve()
    with connect_postgres(args.dsn, autocommit=True, application_name="NEXUS ERP schema installer") as connection:
        digest = install_postgres_baseline_file(
            connection, ddl_path, schema=args.schema
        )
    print(json.dumps({
        "status": "installed",
        "schema": args.schema,
        "baseline_sha256": digest,
        "ddl": str(ddl_path),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
