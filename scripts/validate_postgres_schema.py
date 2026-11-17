"""Validate generated NEXUS DDL against an isolated PostgreSQL database."""
import argparse
import json
import os
import uuid
from pathlib import Path


def validate(ddl_path, dsn):
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
            foreign_key_count = connection.execute(
                """SELECT COUNT(*) FROM information_schema.table_constraints
                   WHERE constraint_schema = %s AND constraint_type = 'FOREIGN KEY'""",
                (schema_name,),
            ).fetchone()[0]
            index_count = connection.execute(
                """SELECT COUNT(*) FROM pg_indexes WHERE schemaname = %s""",
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
            return {
                "schema": schema_name,
                "table_count": table_count,
                "foreign_key_count": foreign_key_count,
                "index_count": index_count,
                "trigger_count": trigger_count,
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
    args = parser.parse_args(argv)
    if not args.dsn:
        parser.error("Provide --dsn or NEXUS_TEST_POSTGRES_DSN.")
    result = validate(args.ddl, args.dsn)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
