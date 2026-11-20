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

from scripts.generate_postgres_schema import generate_postgres_ddl, POSTGRES_TYPE_OVERRIDES


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



def _audit_explicit_numeric_override(connection, table, column, target_type, expected_source_type, *, max_examples):
    """Check that every source value fits its reviewed PostgreSQL NUMERIC(p,s) exactly."""
    qtable, qcolumn = _quote_identifier(table), _quote_identifier(column)
    actual_columns = {}
    for row in connection.execute(f"PRAGMA table_info({qtable})").fetchall():
        name = row["name"] if isinstance(row, sqlite3.Row) else row[1]
        declared_type = row["type"] if isinstance(row, sqlite3.Row) else row[2]
        actual_columns[name] = (declared_type or "").strip().upper()
    actual_type = actual_columns.get(column)
    if actual_type is None or actual_type != expected_source_type:
        return {
            "table": table, "column": column, "target_type": target_type,
            "status": "schema_mismatch", "actual_type": actual_type,
            "expected_source_type": expected_source_type,
        }

    match = re.fullmatch(r"NUMERIC\((\d+),\s*(\d+)\)", target_type.upper())
    if not match:
        return {
            "table": table, "column": column, "target_type": target_type,
            "status": "unbounded_or_unreviewed_target_type",
        }
    precision, scale = int(match.group(1)), int(match.group(2))
    non_null_count = empty_count = valid_count = invalid_count = 0
    min_value = max_value = None
    max_integer_digits = max_fractional_digits = 0
    invalid_sample_positions = []
    cursor = connection.execute(
        f"SELECT {qcolumn} FROM {qtable} WHERE {qcolumn} IS NOT NULL"
    )
    for row in cursor:
        raw = row[0]
        non_null_count += 1
        text_value = repr(raw) if actual_type == "REAL" else str(raw).strip()
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
        shape_value = number.normalize() if number else Decimal("0")
        integer_digits, fractional_digits = _decimal_shape(shape_value)
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
    elif max_integer_digits > precision - scale or max_fractional_digits > scale:
        status = "precision_exceeds_target"
    elif actual_type == "REAL":
        status = "source_binary_float_requires_policy"
    else:
        status = "fits_declared_precision"

    return {
        "table": table, "column": column, "target_type": target_type,
        "precision": precision, "scale": scale, "status": status,
        "actual_type": actual_type, "non_null_count": non_null_count,
        "valid_count": valid_count, "empty_count": empty_count,
        "invalid_count": invalid_count,
        "min_value": str(min_value) if min_value is not None else None,
        "max_value": str(max_value) if max_value is not None else None,
        "max_integer_digits": max_integer_digits,
        "max_fractional_digits": max_fractional_digits,
        "invalid_sample_positions": invalid_sample_positions,
    }


def audit_integer_storage_classes(connection, snapshot, *, max_examples=10):
    """Reject non-integer SQLite storage classes in columns mapped to BIGINT."""
    results = []
    for table in snapshot.get("tables", []):
        table_name = table["name"]
        qtable = _quote_identifier(table_name)
        for column in table.get("columns", []):
            declared = (column.get("type") or "").strip().upper()
            if declared not in {"INTEGER", "BIGINT"}:
                continue
            column_name = column["name"]
            qcolumn = _quote_identifier(column_name)
            actual = connection.execute(
                f"PRAGMA table_info({qtable})"
            ).fetchall()
            actual_type = next(
                ((row["type"] if isinstance(row, sqlite3.Row) else row[2]) or "").strip().upper()
                for row in actual
                if (row["name"] if isinstance(row, sqlite3.Row) else row[1]) == column_name
            ) if any(
                (row["name"] if isinstance(row, sqlite3.Row) else row[1]) == column_name
                for row in actual
            ) else None
            if actual_type != declared:
                results.append({
                    "table": table_name, "column": column_name,
                    "status": "schema_mismatch", "actual_type": actual_type,
                })
                continue
            counts = {}
            for row in connection.execute(
                f"SELECT typeof({qcolumn}) AS storage_class, COUNT(*) AS row_count "
                f"FROM {qtable} WHERE {qcolumn} IS NOT NULL GROUP BY typeof({qcolumn})"
            ).fetchall():
                storage_class = row["storage_class"] if isinstance(row, sqlite3.Row) else row[0]
                row_count = row["row_count"] if isinstance(row, sqlite3.Row) else row[1]
                counts[storage_class] = int(row_count)
            invalid = {kind: count for kind, count in counts.items() if kind != "integer"}
            null_count = int(connection.execute(
                f"SELECT COUNT(*) FROM {qtable} WHERE {qcolumn} IS NULL"
            ).fetchone()[0])
            required_nulls = null_count if column.get("notnull") else 0
            status = (
                "non_integer_storage_class" if invalid
                else "not_null_violation" if required_nulls
                else "integer_storage_only" if counts
                else "no_source_data"
            )
            samples = []
            if invalid:
                samples = [
                    {
                        "rowid": row["rowid"] if isinstance(row, sqlite3.Row) else row[0],
                        "storage_class": row["storage_class"] if isinstance(row, sqlite3.Row) else row[1],
                        "value": str(row["value"] if isinstance(row, sqlite3.Row) else row[2])[:80],
                    }
                    for row in connection.execute(
                        f"SELECT rowid, typeof({qcolumn}) AS storage_class, "
                        f"{qcolumn} AS value FROM {qtable} "
                        f"WHERE {qcolumn} IS NOT NULL AND typeof({qcolumn}) <> 'integer' "
                        f"LIMIT ?", (max_examples,)
                    ).fetchall()
                ]
            results.append({
                "table": table_name, "column": column_name, "status": status,
                "storage_class_counts": counts, "null_count": null_count,
                "invalid_samples": samples,
            })
    return results


def audit_numeric_candidates(connection, snapshot, *, max_examples=10):
    """Audit all TEXT numeric candidates in a schema snapshot against a read-only DB."""
    if max_examples < 0 or max_examples > 100:
        raise ValueError("max_examples must be between 0 and 100.")
    _, review = generate_postgres_ddl(snapshot, require_all_numeric_overrides=False)
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

    snapshot_columns = {
        (table["name"], column["name"]): (column.get("type") or "").strip().upper()
        for table in snapshot.get("tables", [])
        for column in table.get("columns", [])
    }
    snapshot_table_names = {
        table["name"] for table in snapshot.get("tables", [])
    }
    # A deliberately partial source database may not contain whole optional
    # modules (for example, a focused test fixture without currencies). Skip
    # overrides for absent tables, but fail closed if a present table is missing
    # an expected column or its declared source type has drifted.
    override_columns = [
        _audit_explicit_numeric_override(
            connection, table, column, target_type,
            snapshot_columns.get((table, column), ""),
            max_examples=max_examples,
        )
        for (table, column), target_type in sorted(POSTGRES_TYPE_OVERRIDES.items())
        if table in snapshot_table_names
    ]
    integer_columns = audit_integer_storage_classes(
        connection, snapshot, max_examples=max_examples
    )
    integer_status_counts = {}
    for item in integer_columns:
        integer_status_counts[item["status"]] = (
            integer_status_counts.get(item["status"], 0) + 1
        )

    counts = {}
    for item in result_columns:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    override_counts = {}
    for item in override_columns:
        override_counts[item["status"]] = override_counts.get(item["status"], 0) + 1
    safe = (
        bool(result_columns or override_columns)
        and integrity_check == ["ok"]
        and not foreign_key_violations
        # An entirely NULL/empty column has no values to convert. It is safe
        # for this snapshot; blank strings, invalid values, REAL values with
        # data, schema drift, and precision overflow remain blocking.
        and all(item["status"] in {
            "convertible_exact_decimal", "no_source_data"
        } for item in result_columns)
        and all(item["status"] in {
            "fits_declared_precision", "no_source_data"
        } for item in override_columns)
        and all(item["status"] in {
            "integer_storage_only", "no_source_data"
        } for item in integer_columns)
    )
    return {
        "source_read_only": True,
        "integrity_check": integrity_check,
        "foreign_key_violation_count": len(foreign_key_violations),
        "foreign_key_violations": foreign_key_violations[:max_examples],
        "candidate_count": len(candidates),
        "audited_column_count": len(result_columns),
        "override_audited_column_count": len(override_columns),
        "total_audited_column_count": len(result_columns) + len(override_columns),
        "safe_for_numeric_migration": safe,
        "status_counts": counts,
        "override_status_counts": override_counts,
        "integer_storage_class_counts": integer_status_counts,
        "integer_storage_class_audit": integer_columns,
        "columns": result_columns,
        "explicit_numeric_overrides": override_columns,
        "policy": (
            "TEXT candidates are parsed as exact Decimal values. Explicit NUMERIC(p,s) "
            "overrides are separately checked against their declared precision and scale. "
            "No rounding is approved; REAL source columns containing values remain blocked "
            "until a documented binary-float conversion policy is accepted. Columns with no "
            "non-NULL source values are informational and safe for this snapshot. Empty strings, "
            "invalid values, schema drift, and out-of-range values block automatic approval. "
            "SQLite INTEGER/BIGINT affinity columns are also checked by typeof(); text, real, "
            "and blob storage classes block PostgreSQL BIGINT copy."
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
