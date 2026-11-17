"""Generate a complete first-install PostgreSQL baseline from NEXUS modules.

The source schema is initialized in a brand-new temporary SQLite database.
No existing company database or business records are read or modified.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def build_baseline():
    repository_root = Path(__file__).resolve().parents[1]
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))

    from scripts.export_initialized_sqlite_schema import build_initialized_schema
    from scripts.generate_postgres_schema import generate_postgres_ddl

    snapshot = build_initialized_schema()
    ddl, review = generate_postgres_ddl(snapshot)
    review["source"] = snapshot.get("source", {})
    return ddl, review


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        default="build/postgres",
        help="Directory for the generated SQL baseline and review JSON.",
    )
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    ddl, review = build_baseline()

    ddl_path = output_dir / "nexus_full_schema.sql"
    review_path = output_dir / "nexus_full_schema_review.json"
    ddl_path.write_text(ddl, encoding="utf-8")
    review_path.write_text(
        json.dumps(review, ensure_ascii=False, indent=2) + "\\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "ddl": str(ddl_path),
        "review": str(review_path),
        "tables": review["table_count"],
        "columns": review["column_count"],
        "foreign_keys": review["foreign_key_count"],
        "indexes": review["index_count"],
        "triggers": review["trigger_count"],
        "numeric_review_candidates": review["numeric_review_candidate_count"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
