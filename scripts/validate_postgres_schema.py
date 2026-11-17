"""Validate generated NEXUS DDL against an isolated PostgreSQL database."""
import argparse
import json
import os
import uuid
from pathlib import Path


def validate(ddl_path, dsn, expected_review_path=None):
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            "Install psycopg[binary] to validate PostgreSQL DDL."
        ) from exc

    ddl = Path(ddl_path).read_text(encoding="utf-8")
    # Never reuse or drop a fixed schema name: callers may point this tool at
    # a shared development database. A unique, owned schema keeps validation
    # isolated and makes cleanup incapable of deleting another run's objects.
    schema_name = "nexus_schema_validation_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(f'CREATE SCHEMA "{schema_name}"')
        try:
            connection.execute(f'SET search_path TO "{schema_name}"')
            connection.execute(ddl)
            table_count = connection.execute(
                """SELECT COUNT(*) FROM information_schema.tables
                   WHERE table_schema = %s AND table_type = 'BASE TABLE'""",
                (schema_name,),
            ).fetchone()[0]
            column_count = connection.execute(
                """SELECT COUNT(*) FROM information_schema.columns
                   WHERE table_schema = %s""",
                (schema_name,),
            ).fetchone()[0]
            foreign_key_count = connection.execute(
                """SELECT COUNT(*) FROM information_schema.table_constraints
                   WHERE constraint_schema = %s AND constraint_type = 'FOREIGN KEY'""",
                (schema_name,),
            ).fetchone()[0]
            # The generator's review count represents explicit source indexes;
            # PostgreSQL also creates one backing index for each PRIMARY KEY (and
            # UNIQUE constraint). Exclude those constraint-owned indexes here so
            # both sides count the same objects. Keep invalid-index validation
            # separate below, where every physical index must still be valid.
            index_count = connection.execute(
                """SELECT COUNT(*)
                   FROM pg_index i
                   JOIN pg_class index_class ON index_class.oid = i.indexrelid
                   JOIN pg_namespace n ON n.oid = index_class.relnamespace
                   LEFT JOIN pg_constraint constraint_owner
                     ON constraint_owner.conindid = i.indexrelid
                    AND constraint_owner.contype IN ('p', 'u')
                   WHERE n.nspname = %s
                     AND constraint_owner.oid IS NULL""",
                (schema_name,),
            ).fetchone()[0]
            trigger_count = connection.execute(
                """SELECT COUNT(*) FROM pg_trigger t
                   JOIN pg_class c ON c.oid = t.tgrelid
                   JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname = %s AND NOT t.tgisinternal""",
                (schema_name,),
            ).fetchone()[0]
            invalid_indexes = connection.execute(
                """SELECT COUNT(*) FROM pg_index i
                   JOIN pg_class c ON c.oid = i.indexrelid
                   JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname = %s AND NOT i.indisvalid""",
                (schema_name,),
            ).fetchone()[0]
            if invalid_indexes:
                raise AssertionError(f"{invalid_indexes} invalid PostgreSQL indexes.")
            actual_counts = {
                "table_count": table_count,
                "column_count": column_count,
                "foreign_key_count": foreign_key_count,
                "index_count": index_count,
                "trigger_count": trigger_count,
            }
            if expected_review_path:
                expected_review = json.loads(Path(expected_review_path).read_text(encoding="utf-8"))
                mismatches = {
                    key: {"expected": expected_review.get(key), "actual": actual_counts[key]}
                    for key in actual_counts
                    if not isinstance(expected_review.get(key), int)
                    or expected_review[key] != actual_counts[key]
                }
                if mismatches:
                    raise AssertionError(
                        "Generated PostgreSQL schema counts do not match the source review: "
                        + json.dumps(mismatches, ensure_ascii=False, sort_keys=True)
                    )
            return {
                "schema": schema_name,
                **actual_counts,
                "expected_counts_verified": bool(expected_review_path),
                "invalid_index_count": invalid_indexes,
            }
        except Exception:
            # Roll back the explicit DDL transaction before dropping the
            # disposable schema when PostgreSQL rejects any statement.
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ddl", required=True, help="Generated PostgreSQL DDL file")
    parser.add_argument(
        "--dsn",
        default=os.environ.get("NEXUS_TEST_POSTGRES_DSN", ""),
        help="Disposable PostgreSQL test database DSN",
    )
    parser.add_argument(
        "--expected-review",
        default="",
        help="Optional generator review JSON; verify PostgreSQL object counts match it",
    )
    args = parser.parse_args(argv)
    if not args.dsn:
        parser.error("Provide --dsn or NEXUS_TEST_POSTGRES_DSN.")
    result = validate(args.ddl, args.dsn, args.expected_review or None)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
