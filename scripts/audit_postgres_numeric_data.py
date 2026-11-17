"""Read-only audit of SQLite TEXT columns proposed for PostgreSQL NUMERIC.

This tool validates source values before a later migration changes column types.
It never modifies the SQLite source database or PostgreSQL.
"""
import argparse
from decimal import Decimal, InvalidOperation
import json
import re
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import quote

from scripts.generate_postgres_schema import generate_postgres_ddl


_DECIMAL_INPUT = re.compile(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")
POSTGRES_NUMERIC_MAX_INTEGER_DIGITS = 131072
POSTGRES_NUMERIC_MAX_FRACTIONAL_DIGITS = 16383


def _quote_identifier(value):
    value = str(value)
    if not value or not (value[0].isalpha() or value[0] == "_"):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    if not all(char.isalnum() or char == "_" for char in value):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return '"' + value.replace('"', '""') + '"'


def _decimal_shape(value):
    """Return integer/fraction digit counts without rounding or float conversion."""
    parts = value.as_tuple()
    digits = len(parts.digits)
    scale = max(0, -parts.exponent)
    integer_digits = max(0, digits + parts.exponent)
    return integer_digits, scale


def audit_numeric_candidates(connection, snapshot, *, max_examples=10):
    """Audit all TEXT numeric candidates in a schema snapshot against a read-only DB."""
    if max_examples < 0 or max_examples > 100:
        raise ValueError("max_examples must be between 0 and 100.")
    _, review = generate_postgres_ddl(snapshot)
    integrity_check = [row[0] for row in connection.execute("PRAGMA integrity_check").fetchall()]
    foreign_key_violations = [
        {
            "table": row["table"] if isinstance(row, sqlite3.Row) else row[0],
            "rowid": row["rowid"] if isinstance(row, sqlite3.Row) else row[1],
            "parent": row["parent"] if isinstance(row, sqlite3.Row) else row[2],
            "fkid": row["fkid"] if isinstance(row, sqlite3.Row) else row[3],
        }
        for row in connection.execute("PRAGMA foreign_key_check").fetchall()
    ]
    candidates = [
        item for item in review["numeric_review_candidates"]
        if item["source_type"] == "TEXT" and item["postgres_type"] == "TEXT"
    ]
    result_columns = []
    for item in candidates:
        table = item["table"]
        column = item["column"]
        qtable, qcolumn = _quote_identifier(table), _quote_identifier(column)
        actual_columns = {}
        for row in connection.execute(f"PRAGMA table_info({qtable})").fetchall():
            name = row["name"] if isinstance(row, sqlite3.Row) else row[1]
            declared_type = row["type"] if isinstance(row, sqlite3.Row) else row[2]
            actual_columns[name] = (declared_type or "").strip().upper()
        if column not in actual_columns:
            result_columns.append({
                "table": table, "column": column, "status": "schema_mismatch",
                "reason": "Column is missing from the supplied SQLite database.",
            })
            continue
        actual_type = actual_columns[column]
        if actual_type != "TEXT":
            result_columns.append({
                "table": table, "column": column, "status": "schema_mismatch",
                "actual_type": actual_type,
                "reason": "Column type differs from the initialized schema snapshot.",
            })
            continue

        non_null_count = 0
        empty_count = 0
        valid_count = 0
        invalid_count = 0
        min_value = None
        max_value = None
        max_integer_digits = 0
        max_fractional_digits = 0
        invalid_sample_positions = []
        cursor = connection.execute(f"SELECT {qcolumn} FROM {qtable} WHERE {qcolumn} IS NOT NULL")
        for row in cursor:
            raw = row[0]
            non_null_count += 1
            text_value = str(raw).strip()
            if not text_value:
                empty_count += 1
                continue
            try:
                if not _DECIMAL_INPUT.fullmatch(text_value):
                    raise InvalidOperation
                number = Decimal(text_value)
                if not number.is_finite():
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                invalid_count += 1
                if len(invalid_sample_positions) < max_examples:
                    invalid_sample_positions.append(non_null_count)
                continue
            valid_count += 1
            integer_digits, fractional_digits = _decimal_shape(number)
            max_integer_digits = max(max_integer_digits, integer_digits)
            max_fractional_digits = max(max_fractional_digits, fractional_digits)
            if min_value is None or number < min_value:
                min_value = number
            if max_value is None or number > max_value:
                max_value = number

        if invalid_count:
            status = "invalid_values"
        elif empty_count:
            status = "empty_values_require_policy"
        elif non_null_count == 0:
            status = "no_source_data"
        elif (
            max_integer_digits > POSTGRES_NUMERIC_MAX_INTEGER_DIGITS
            or max_fractional_digits > POSTGRES_NUMERIC_MAX_FRACTIONAL_DIGITS
        ):
            status = "postgres_numeric_limit_exceeded"
        else:
            status = "convertible_exact_decimal"
        result_columns.append({
            "table": table,
            "column": column,
            "status": status,
            "actual_type": actual_type,
            "non_null_count": non_null_count,
            "valid_count": valid_count,
            "empty_count": empty_count,
            "invalid_count": invalid_count,
            "min_value": str(min_value) if min_value is not None else None,
            "max_value": str(max_value) if max_value is not None else None,
            "max_integer_digits": max_integer_digits,
            "max_fractional_digits": max_fractional_digits,
            "invalid_sample_positions": invalid_sample_positions,
        })

    counts = {}
    for item in result_columns:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    safe = (
        bool(result_columns)
        and integrity_check == ["ok"]
        and not foreign_key_violations
        and all(item["status"] == "convertible_exact_decimal" for item in result_columns)
    )
    return {
        "source_read_only": True,
        "integrity_check": integrity_check,
        "foreign_key_violation_count": len(foreign_key_violations),
        "foreign_key_violations": foreign_key_violations[:max_examples],
        "candidate_count": len(candidates),
        "audited_column_count": len(result_columns),
        "safe_for_numeric_migration": safe,
        "status_counts": counts,
        "columns": result_columns,
        "policy": (
            "No rounding or float conversion is performed. Empty strings, invalid values, "
            "schema drift, no source rows, and PostgreSQL NUMERIC digit-limit overflow "
            "block automatic approval. Invalid values are identified by sample position, "
            "not copied into the report."
        ),
    }


def _open_read_only_database(database_path, *, allow_non_temp=False):
    path = Path(database_path).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError("Database path must point to an existing SQLite file.")
    if not allow_non_temp:
        temp_root = Path(tempfile.gettempdir()).resolve()
        try:
            path.relative_to(temp_root)
        except ValueError as exc:
            raise ValueError(
                "Refusing to inspect a database outside the OS temporary directory. "
                "Use --allow-non-temp-read-only only for an intentional read-only audit."
            ) from exc
    uri_path = quote(path.as_posix(), safe="/:")
    connection = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection, path


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Read-only audit of SQLite decimal values before PostgreSQL NUMERIC migration."
    )
    parser.add_argument("--database", required=True, help="SQLite database to audit")
    parser.add_argument("--schema", required=True, help="Initialized SQLite schema JSON snapshot")
    parser.add_argument("--output", help="Optional JSON report path")
    parser.add_argument("--max-examples", type=int, default=10)
    parser.add_argument(
        "--allow-non-temp-read-only", action="store_true",
        help="Explicitly permit a source outside the OS temp folder; connection remains read-only.",
    )
    args = parser.parse_args(argv)
    connection = None
    try:
        snapshot = json.loads(Path(args.schema).expanduser().read_text(encoding="utf-8"))
        connection, path = _open_read_only_database(
            args.database, allow_non_temp=args.allow_non_temp_read_only
        )
        report = audit_numeric_candidates(connection, snapshot, max_examples=args.max_examples)
        report["source_database"] = path.name
        report["source_path_policy"] = (
            "explicit_non_temp_read_only" if args.allow_non_temp_read_only else "temporary_directory_only"
        )
        encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            output = Path(args.output).expanduser().resolve()
            if output == path:
                raise ValueError("Report output must not overwrite the read-only source database.")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(encoded, encoding="utf-8")
            print(json.dumps({
                "output": str(output),
                "candidate_count": report["candidate_count"],
                "safe_for_numeric_migration": report["safe_for_numeric_migration"],
                "status_counts": report["status_counts"],
            }, ensure_ascii=False))
        else:
            print(encoded, end="")
        return 0 if report["safe_for_numeric_migration"] else 2
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
