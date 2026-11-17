"""Build a clean, fully initialized SQLite schema snapshot in an OS temp directory.

No existing NEXUS database is opened. All application paths are redirected to a
fresh temporary directory before importing app.py.
"""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path


def build_initialized_schema():
    repository_root = Path(__file__).resolve().parents[1]
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))

    with tempfile.TemporaryDirectory(prefix="nexus-postgresql-schema-") as temp_root:
        temp_path = Path(temp_root)
        data_dir = temp_path / "data"
        backup_dir = temp_path / "backups"

        # These values are set before importing app_paths or app.py.
        os.environ["NEXUS_DATA_DIR"] = str(data_dir)
        os.environ["NEXUS_BACKUP_DIR"] = str(backup_dir)
        os.environ["NEXUS_DEMO_MODE"] = "1"
        os.environ["NEXUS_SKIP_DB_INIT"] = "1"
        os.environ["NEXUS_SECRET_KEY"] = "schema-inventory-test-key-" + ("x" * 40)

        import app as nexus_app
        from scripts.inspect_sqlite_schema import inspect_database

        # initialize_database() runs the real application schema initializers,
        # but only against the newly-created temporary database.
        nexus_app.initialize_database()
        result = inspect_database(nexus_app.DB_PATH, include_row_counts=False)
        result["source"] = {
            "kind": "fresh_initialized_application_schema",
            "database_engine": "sqlite",
            "business_data_included": False,
            "database_path": "temporary-only",
        }
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Export the fully initialized NEXUS SQLite schema from a fresh temporary DB."
    )
    parser.add_argument(
        "--output",
        help="Optional JSON output file. If omitted, write the schema to stdout.",
    )
    args = parser.parse_args(argv)

    result = build_initialized_schema()
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(encoded, encoding="utf-8")
        print(json.dumps({
            "output": str(output_path),
            "summary": result["summary"],
            "foreign_key_violations": len(result.get("foreign_key_violations", [])),
        }, ensure_ascii=False))
    else:
        sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
