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
            actual_foreign_keys = connection.execute(
                """
                SELECT src.relname,
                       array_agg(src_att.attname ORDER BY ord.ordinality),
                       tgt.relname,
                       array_agg(tgt_att.attname ORDER BY ord.ordinality),
                       con.confupdtype,
                       con.confdeltype
                  FROM pg_constraint con
                  JOIN pg_class src ON src.oid = con.conrelid
                  JOIN pg_namespace src_ns ON src_ns.oid = src.relnamespace
                  JOIN pg_class tgt ON tgt.oid = con.confrelid
                  JOIN LATERAL unnest(con.conkey, con.confkey)
                       WITH ORDINALITY AS ord(source_attnum, target_attnum, ordinality)
                    ON TRUE
                  JOIN pg_attribute src_att
                    ON src_att.attrelid = src.oid AND src_att.attnum = ord.source_attnum
                  JOIN pg_attribute tgt_att
                    ON tgt_att.attrelid = tgt.oid AND tgt_att.attnum = ord.target_attnum
                 WHERE con.contype = 'f' AND src_ns.nspname = %s
                 GROUP BY src.relname, tgt.relname, con.conname,
                          con.confupdtype, con.confdeltype
                """,
                (schema_name,),
            ).fetchall()
            action_names = {
                "a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE",
                "n": "SET NULL", "d": "SET DEFAULT",
            }
            actual_fk_manifest = sorted(
                [
                    {
                        "table": row[0],
                        "columns": list(row[1]),
                        "referenced_table": row[2],
                        "referenced_columns": list(row[3]),
                        "on_update": action_names[row[4]],
                        "on_delete": action_names[row[5]],
                    }
                    for row in actual_foreign_keys
                ],
                key=lambda item: (
                    item["table"], tuple(item["columns"]),
                    item["referenced_table"], tuple(item["referenced_columns"]),
                ),
            )
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
                expected_fk_manifest = expected_review.get("foreign_keys")
                if not isinstance(expected_fk_manifest, list):
                    raise AssertionError(
                        "Schema review is missing the exact foreign_keys manifest."
                    )
                expected_fk_manifest = sorted(
                    expected_fk_manifest,
                    key=lambda item: (
                        item["table"], tuple(item["columns"]),
                        item["referenced_table"], tuple(item["referenced_columns"]),
                    ),
                )
                if expected_fk_manifest != actual_fk_manifest:
                    raise AssertionError(
                        "Generated PostgreSQL foreign keys differ from the source review: "
                        + json.dumps({
                            "expected": expected_fk_manifest[:200],
                            "actual": actual_fk_manifest[:200],
                        }, ensure_ascii=False, sort_keys=True)
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
