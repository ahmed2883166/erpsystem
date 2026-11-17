"""Read-only SQLite schema inventory for isolated NEXUS migration tests.

The tool deliberately refuses databases outside the operating-system temporary
folder. It never creates, modifies, or migrates a database.
"""
import argparse
import json
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import quote


def _quote_identifier(value):
    return '"' + str(value).replace('"', '""') + '"'


def _temporary_database_path(value):
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError("Database path must point to an existing file.")
    temp_root = Path(tempfile.gettempdir()).resolve()
    try:
        path.relative_to(temp_root)
    except ValueError as exc:
        raise ValueError(
            "Refusing to inspect a database outside the OS temporary directory."
        ) from exc
    return path


def inspect_database(database_path, include_row_counts=False):
    """Return schema metadata and integrity checks; never return row values."""
    path = _temporary_database_path(database_path)
    uri_path = quote(path.as_posix(), safe="/:")
    connection = sqlite3.connect(
        f"file:{uri_path}?mode=ro", uri=True, timeout=5
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        objects = connection.execute(
            """SELECT type, name, tbl_name, sql
               FROM sqlite_master
               WHERE type IN ('table', 'view', 'index', 'trigger')
                 AND name NOT LIKE 'sqlite_%'
               ORDER BY type, name"""
        ).fetchall()
        tables = []
        views = []
        indexes = []
        triggers = []
        for obj in objects:
            item = {
                "name": obj["name"],
                "table_name": obj["tbl_name"],
                "sql": obj["sql"],
            }
            if obj["type"] == "view":
                views.append(item)
                continue
            if obj["type"] == "index":
                indexes.append(item)
                continue
            if obj["type"] == "trigger":
                triggers.append(item)
                continue

            table = dict(item)
            table_name = obj["name"]
            quoted = _quote_identifier(table_name)
            table["columns"] = [
                dict(row) for row in connection.execute(
                    f"PRAGMA table_info({quoted})"
                ).fetchall()
            ]
            table["foreign_keys"] = [
                dict(row) for row in connection.execute(
                    f"PRAGMA foreign_key_list({quoted})"
                ).fetchall()
            ]
            table_indexes = connection.execute(
                f"PRAGMA index_list({quoted})"
            ).fetchall()
            table["indexes"] = []
            for index_row in table_indexes:
                index = dict(index_row)
                index_name = index_row["name"]
                index["columns"] = [
                    dict(row) for row in connection.execute(
                        f"PRAGMA index_info({_quote_identifier(index_name)})"
                    ).fetchall()
                ]
                table["indexes"].append(index)
            if include_row_counts:
                table["row_count"] = connection.execute(
                    f"SELECT COUNT(*) FROM {quoted}"
                ).fetchone()[0]
            tables.append(table)

        foreign_key_violations = [
            dict(row) for row in connection.execute("PRAGMA foreign_key_check")
        ]
        integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
        integrity_check = [row[0] for row in integrity_rows]
        return {
            "database_file": path.name,
            "sqlite_version": sqlite3.sqlite_version,
            "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
            "tables": tables,
            "views": views,
            "indexes": indexes,
            "triggers": triggers,
            "foreign_key_violations": foreign_key_violations,
            "integrity_check": integrity_check,
            "summary": {
                "table_count": len(tables),
                "view_count": len(views),
                "index_count": len(indexes),
                "trigger_count": len(triggers),
                "column_count": sum(len(table["columns"]) for table in tables),
                "foreign_key_count": sum(len(table["foreign_keys"]) for table in tables),
            },
        }
    finally:
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Export SQLite schema metadata from an isolated temporary DB."
    )
    parser.add_argument("--database", required=True, help="Existing temporary SQLite DB")
    parser.add_argument(
        "--include-row-counts",
        action="store_true",
        help="Include table row counts (may scan large tables).",
    )
    args = parser.parse_args(argv)
    try:
        result = inspect_database(args.database, args.include_row_counts)
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
