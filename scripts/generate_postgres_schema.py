"""Generate a conservative PostgreSQL DDL baseline from SQLite schema JSON.

This is a schema-porting aid, not an application database adapter. It preserves
SQLite TEXT values as PostgreSQL TEXT (including exact Decimal strings) until
each numeric field has an explicitly reviewed precision/scale mapping.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path


IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def quote_ident(value):
    value = str(value)
    if not IDENTIFIER.fullmatch(value):
        raise ValueError(f"Unsafe SQL identifier in schema snapshot: {value!r}")
    return '"' + value + '"'


def short_identifier(value, limit=63):
    value = str(value)
    if len(value) <= limit:
        return value
    suffix = hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]
    return value[:limit - len(suffix) - 1] + "_" + suffix


# Explicit, reviewed overrides only. Unlisted columns keep conservative SQLite
# affinity mapping; NUMERIC without a typmod avoids silently rounding legacy
# decimal strings while preserving PostgreSQL arithmetic semantics.
POSTGRES_TYPE_OVERRIDES = {
    ("journal_lines", "debit"): "NUMERIC",
    ("journal_lines", "credit"): "NUMERIC",
    ("currencies", "rate_to_base"): "NUMERIC",
    ("tax_rates", "rate"): "NUMERIC",
}


def pg_type(column, table_name=None):
    column_name = column.get("name")
    override = POSTGRES_TYPE_OVERRIDES.get((table_name, column_name))
    if override:
        return override
    declared = (column.get("type") or "").strip().upper()
    if declared == "INTEGER":
        return "BIGINT"
    if declared == "REAL":
        return "DOUBLE PRECISION"
    if declared == "BLOB":
        return "BYTEA"
    # Unreviewed TEXT fields remain TEXT; name heuristics never change types.
    return "TEXT"


def default_sql(value, target_type=None):
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    if value.upper() in ("CURRENT_TIMESTAMP", "CURRENT_DATE", "CURRENT_TIME"):
        expression = value.upper()
        return f"({expression}::text)" if target_type == "TEXT" else expression
    if ";" in value or "--" in value or "/*" in value:
        raise ValueError(f"Unexpected SQL syntax in column default: {value!r}")
    return value


def extract_check_constraints(create_sql):
    """Extract simple balanced CHECK(...) clauses from SQLite CREATE TABLE SQL."""
    if not create_sql:
        return []
    checks = []
    pattern = re.compile(r"\bCHECK\s*\(", re.I)
    for match in pattern.finditer(create_sql):
        start = match.end()
        depth = 1
        quote = None
        i = start
        while i < len(create_sql) and depth:
            char = create_sql[i]
            if quote:
                if char == quote:
                    if i + 1 < len(create_sql) and create_sql[i + 1] == quote:
                        i += 1
                    else:
                        quote = None
            elif char in ("'", '"', "`"):
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            i += 1
        if depth:
            raise ValueError("Unbalanced CHECK expression in SQLite schema SQL.")
        expression = create_sql[start:i - 1].strip()
        if not expression or ";" in expression or "--" in expression or "/*" in expression:
            raise ValueError("Unsupported or unsafe CHECK expression in SQLite schema SQL.")
        checks.append(expression)
    return checks


def extract_nocase_unique_columns(create_sql):
    """Find SQLite columns whose UNIQUE constraint uses COLLATE NOCASE."""
    if not create_sql:
        return set()
    pattern = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s+[^,]*?COLLATE\s+NOCASE[^,]*?\bUNIQUE\b', re.I | re.S)
    return {match.group(1) for match in pattern.finditer(create_sql)}


def _index_sql_by_name(snapshot):
    return {
        item["name"]: item.get("sql")
        for item in snapshot.get("indexes", [])
        if item.get("name") and item.get("sql")
    }


def generate_trigger_ddl(triggers):
    """Translate the known NEXUS SQLite triggers; fail closed on unknown ones."""
    if not triggers:
        return []
    by_name = {trigger["name"]: trigger for trigger in triggers}
    known_audit = {
        "audit_log_block_delete": ("nexus_block_audit_log_mutation", "DELETE"),
        "audit_log_block_update": ("nexus_block_audit_log_mutation", "UPDATE"),
        "budget_audit_no_delete": ("nexus_block_budget_audit_mutation", "DELETE"),
        "budget_audit_no_update": ("nexus_block_budget_audit_mutation", "UPDATE"),
    }
    dashboard_pattern = re.compile(
        r"trg_dashboard_version_([A-Za-z_][A-Za-z0-9_]*)_(insert|update|delete)$"
    )
    stock_triggers = {
        "trg_stock_product_locations_same_wh_insert": ("INSERT", "stock_product_locations"),
        "trg_stock_product_locations_same_wh_update": ("UPDATE", "stock_product_locations"),
    }
    stock_location_deactivation = "trg_stock_locations_prevent_default_deactivation"
    dashboard = []
    for trigger in triggers:
        name = trigger["name"]
        if name in known_audit or name in stock_triggers or name == stock_location_deactivation:
            continue
        match = dashboard_pattern.fullmatch(name)
        if match and match.group(1) == trigger.get("table_name"):
            dashboard.append((trigger, match.group(2).upper()))
            continue
        raise ValueError(f"Unsupported SQLite trigger; refusing to omit it: {name}")

    lines = []
    if any(name in by_name for name in known_audit):
        lines.extend([
            "CREATE OR REPLACE FUNCTION nexus_block_audit_log_mutation()",
            "RETURNS trigger LANGUAGE plpgsql AS $$",
            "BEGIN",
            "    RAISE EXCEPTION 'audit_log is append-only';",
            "END;",
            "$$;",
            "",
            "CREATE OR REPLACE FUNCTION nexus_block_budget_audit_mutation()",
            "RETURNS trigger LANGUAGE plpgsql AS $$",
            "BEGIN",
            "    RAISE EXCEPTION 'Budget audit history is immutable';",
            "END;",
            "$$;",
            "",
        ])
        for name, (function_name, operation) in known_audit.items():
            trigger = by_name.get(name)
            if trigger:
                lines.append(
                    f"CREATE TRIGGER {quote_ident(name)} BEFORE {operation} ON "
                    f"{quote_ident(trigger['table_name'])} FOR EACH ROW "
                    f"EXECUTE FUNCTION {function_name}();"
                )

    if dashboard:
        lines.extend([
            "CREATE OR REPLACE FUNCTION nexus_bump_dashboard_version()",
            "RETURNS trigger LANGUAGE plpgsql AS $$",
            "BEGIN",
            "    UPDATE management_dashboard_data_version",
            "       SET version = version + 1 WHERE id = 1;",
            "    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;",
            "    RETURN NEW;",
            "END;",
            "$$;",
            "",
        ])
        for trigger, operation in dashboard:
            lines.append(
                f"CREATE TRIGGER {quote_ident(trigger['name'])} AFTER {operation} ON "
                f"{quote_ident(trigger['table_name'])} FOR EACH ROW "
                "EXECUTE FUNCTION nexus_bump_dashboard_version();"
            )

    if any(name in by_name for name in stock_triggers):
        lines.extend([
            "CREATE OR REPLACE FUNCTION nexus_validate_stock_product_location()",
            "RETURNS trigger LANGUAGE plpgsql AS $$",
            "BEGIN",
            "    IF NOT EXISTS (",
            "        SELECT 1 FROM stock_locations l",
            "         WHERE l.id = NEW.location_id",
            "           AND l.warehouse_id = NEW.warehouse_id",
            "           AND l.active = 1",
            "    ) THEN",
            "        RAISE EXCEPTION 'موقع الصنف الافتراضي يجب أن يكون نشطًا ويتبع نفس المخزن.';",
            "    END IF;",
            "    RETURN NEW;",
            "END;",
            "$$;",
            "",
        ])
        for name, (operation, table_name) in stock_triggers.items():
            if name not in by_name:
                continue
            timing = "BEFORE UPDATE OF warehouse_id, location_id" if operation == "UPDATE" else "BEFORE INSERT"
            lines.append(
                f"CREATE TRIGGER {quote_ident(name)} {timing} ON "
                f"{quote_ident(table_name)} FOR EACH ROW "
                "EXECUTE FUNCTION nexus_validate_stock_product_location();"
            )
    if stock_location_deactivation in by_name:
        trigger = by_name[stock_location_deactivation]
        if trigger.get("table_name") != "stock_locations":
            raise ValueError("Stock location deactivation trigger is attached to an unexpected table.")
        lines.extend([
            "CREATE OR REPLACE FUNCTION nexus_prevent_default_location_deactivation()",
            "RETURNS trigger LANGUAGE plpgsql AS $$",
            "BEGIN",
            "    IF OLD.active = 1 AND NEW.active = 0 AND EXISTS (",
            "        SELECT 1 FROM stock_product_locations p WHERE p.location_id = OLD.id",
            "    ) THEN",
            "        RAISE EXCEPTION 'لا يمكن تعطيل موقع مستخدم كموقع افتراضي لصنف. غيّر الموقع الافتراضي أولًا.';",
            "    END IF;",
            "    RETURN NEW;",
            "END;",
            "$$;",
            "",
            f"CREATE TRIGGER {quote_ident(stock_location_deactivation)} BEFORE UPDATE OF active ON stock_locations "
            "FOR EACH ROW EXECUTE FUNCTION nexus_prevent_default_location_deactivation();",
        ])
    return lines


def generate_postgres_ddl(snapshot):
    tables = snapshot.get("tables")
    if not isinstance(tables, list) or not tables:
        raise ValueError("Schema snapshot contains no tables.")
    views = snapshot.get("views", [])
    if views:
        view_names = sorted(
            str(view.get("name") or "<unnamed>") for view in views
        )
        raise ValueError(
            "SQLite views are not translated to PostgreSQL yet; refusing to "
            "generate an incomplete schema. Review views: " + ", ".join(view_names)
        )

    lines = [
        "-- Generated conservative PostgreSQL baseline from NEXUS SQLite schema.",
        "-- Only explicitly reviewed monetary/rate fields use PostgreSQL NUMERIC; other TEXT fields remain TEXT.",
        "-- Review NUMERIC precision/scale mappings separately before application cutover.",
        "BEGIN;",
        "",
    ]
    table_names = {table["name"] for table in tables}
    columns_by_table = {
        table["name"]: {column.get("name") for column in table.get("columns", [])}
        for table in tables
    }
    primary_columns_by_table = {
        table["name"]: [
            column["name"]
            for column in sorted(
                (item for item in table.get("columns", []) if int(item.get("pk") or 0)),
                key=lambda item: int(item.get("pk") or 0),
            )
        ]
        for table in tables
    }
    unknown_overrides = sorted(
        f"{table_name}.{column_name}"
        for table_name, column_name in POSTGRES_TYPE_OVERRIDES
        if table_name not in columns_by_table
        or column_name not in columns_by_table[table_name]
    )
    if unknown_overrides:
        raise ValueError(
            "PostgreSQL type overrides reference missing schema columns: "
            + ", ".join(unknown_overrides)
        )
    pending_fks = []
    pending_indexes = []
    nocase_unique_columns = {table['name']: extract_nocase_unique_columns(table.get('sql')) for table in tables}
    numeric_candidates = []
    numeric_review_candidates = []
    identity_columns = []

    for table in tables:
        table_name = table["name"]
        qtable = quote_ident(table_name)
        columns = table.get("columns", [])
        if not columns:
            raise ValueError(f"Table {table_name!r} has no columns in snapshot.")
        hidden_columns = [
            column.get("name")
            for column in columns
            if int(column.get("hidden") or 0) != 0
        ]
        if hidden_columns:
            raise ValueError(
                f"SQLite generated/virtual hidden columns are not translated to PostgreSQL yet; "
                f"refusing to omit {table_name}: " + ", ".join(map(str, hidden_columns))
            )
        primary = sorted(
            (column for column in columns if int(column.get("pk") or 0)),
            key=lambda column: int(column.get("pk") or 0),
        )
        definitions = []
        for column in columns:
            name = column["name"]
            qname = quote_ident(name)
            is_single_integer_pk = (
                len(primary) == 1 and primary[0]["name"] == name
                and (column.get("type") or "").strip().upper() == "INTEGER"
            )
            definition = f"    {qname} {pg_type(column, table_name)}"
            if is_single_integer_pk:
                definition += " GENERATED BY DEFAULT AS IDENTITY"
                identity_columns.append(f"{table_name}.{name}")
            if len(primary) == 1 and primary[0]["name"] == name:
                definition += " PRIMARY KEY"
            if column.get("notnull") and not is_single_integer_pk:
                definition += " NOT NULL"
            default = default_sql(column.get("dflt_value"), pg_type(column, table_name))
            if default is not None:
                definition += f" DEFAULT {default}"
            definitions.append(definition)

            col_name = name.lower()
            name_parts = col_name.split("_")
            non_numeric_metadata_columns = {
                ("accounts", "normal_balance"),
                ("company_settings", "setting_value"),
                ("management_dashboard_meta", "meta_value"),
                ("sales_credit_notes", "tax_reference"),
                ("sales_invoices", "tax_reference"),
                ("supplier_bills", "tax_reference"),
                ("supplier_credit_notes", "tax_reference"),
            }
            metadata_suffixes = {
                "at", "code", "date", "id", "json", "method", "name", "no",
                "notes", "number", "reason", "status", "treatment", "type",
            }
            numeric_name_match = (
                bool(name_parts)
                and (table_name, name) not in non_numeric_metadata_columns
                and name_parts[-1] not in metadata_suffixes
                and any(
                    token in name_parts for token in (
                        "amount", "debit", "credit", "balance", "price", "cost",
                        "salary", "wage", "tax", "discount", "quantity", "qty",
                        "rate", "value", "subtotal", "total", "paid", "deduction",
                        "allowance", "proceeds", "depreciation", "insured", "reorder",
                    )
                )
            )
            source_type = (column.get("type") or "").strip().upper()
            target_type = pg_type(column, table_name)
            if numeric_name_match and source_type in {"TEXT", "REAL"} and target_type != "NUMERIC":
                numeric_review_candidates.append({
                    "table": table_name,
                    "column": name,
                    "source_type": source_type,
                    "postgres_type": target_type,
                    "reason": (
                        "SQLite REAL may carry binary floating-point monetary values."
                        if source_type == "REAL"
                        else "SQLite TEXT may contain decimal values requiring exact NUMERIC semantics."
                    ),
                })
                if source_type == "TEXT":
                    numeric_candidates.append(f"{table_name}.{name}")

        if primary and len(primary) > 1:
            pk_cols = ", ".join(quote_ident(c["name"]) for c in primary)
            definitions.append(f"    PRIMARY KEY ({pk_cols})")

        for check in extract_check_constraints(table.get("sql")):
            definitions.append(f"    CHECK ({check})")

        lines.append(f"CREATE TABLE {qtable} (")
        lines.append(",\n".join(definitions))
        lines.append(");")
        lines.append("")

        foreign_key_groups = {}
        for fk_index, fk in enumerate(table.get("foreign_keys", [])):
            # PRAGMA foreign_key_list emits one row per column in a composite
            # FK. Group by its stable constraint id before producing PostgreSQL
            # DDL; emitting each row separately silently changes the relation.
            constraint_id = fk.get("id")
            group_key = ("id", constraint_id) if constraint_id is not None else ("row", fk_index)
            foreign_key_groups.setdefault(group_key, []).append(fk)

        for group_key, fk_parts in foreign_key_groups.items():
            fk_parts.sort(key=lambda item: int(item.get("seq") or 0))
            target_table = fk_parts[0]["table"]
            if target_table not in table_names:
                raise ValueError(
                    f"Foreign key {table_name}.{fk_parts[0]['from']} references missing table {target_table}."
                )
            for part in fk_parts:
                if (
                    part.get("table") != target_table
                    or str(part.get("on_update") or "NO ACTION").upper()
                    != str(fk_parts[0].get("on_update") or "NO ACTION").upper()
                    or str(part.get("on_delete") or "NO ACTION").upper()
                    != str(fk_parts[0].get("on_delete") or "NO ACTION").upper()
                ):
                    raise ValueError(
                        f"Composite foreign key {table_name} has inconsistent target or actions."
                    )
                if part.get("from") not in columns_by_table[table_name]:
                    raise ValueError(
                        f"Foreign key source column {table_name}.{part.get('from')} is missing."
                    )

            # SQLite permits REFERENCES parent_table without naming the target
            # columns. Resolve that shorthand to the parent's ordered primary key.
            target_columns = [part.get("to") for part in fk_parts]
            if any(value in (None, "") for value in target_columns):
                if not all(value in (None, "") for value in target_columns):
                    raise ValueError(
                        f"Foreign key {table_name} mixes implicit and explicit target columns."
                    )
                target_columns = primary_columns_by_table.get(target_table, [])
            if len(target_columns) != len(fk_parts) or not target_columns:
                raise ValueError(
                    f"Foreign key {table_name} has an unresolved target primary key on {target_table}."
                )
            if any(column not in columns_by_table[target_table] for column in target_columns):
                raise ValueError(
                    f"Foreign key {table_name} references a missing column on {target_table}."
                )
            pending_fks.append((table_name, fk_parts, target_columns))

        for index in table.get("indexes", []):
            if index.get("origin") == "pk":
                continue
            cols = [column.get("name") for column in index.get("columns", [])]
            pending_indexes.append((table_name, index, cols))

    named_index_sql = _index_sql_by_name(snapshot)
    for table_name, index, cols in pending_indexes:
        name = index.get("name") or f"ix_{table_name}_{'_'.join(str(c) for c in cols)}"
        name = short_identifier(name)
        qindex = quote_ident(name)
        original_sql = named_index_sql.get(index.get("name"))
        # Expression indexes cannot be reconstructed from PRAGMA index_info
        # (SQLite reports cid=-2 and a NULL column name); preserve their SQL.
        if any(column is None for column in cols):
            if not original_sql:
                raise ValueError(f"Expression index {name} has no source SQL.")
            if ";" in original_sql.rstrip().rstrip(";") or "--" in original_sql or "/*" in original_sql:
                raise ValueError(f"Unsafe expression-index SQL for {name}.")
            lines.append(original_sql.strip().rstrip(";") + ";")
            continue
        if not cols:
            raise ValueError(f"Index {name} has no columns.")
        unique = "UNIQUE " if index.get("unique") else ""
        if index.get("unique") and len(cols) == 1 and cols[0] in nocase_unique_columns.get(table_name, set()):
            index_name = quote_ident(short_identifier("uq_" + table_name + "_" + cols[0] + "_nocase"))
            lines.append(
                f"CREATE UNIQUE INDEX {index_name} ON {quote_ident(table_name)} "
                f"(lower({quote_ident(cols[0])}));"
            )
            continue
        where_clause = ""
        if original_sql:
            where_match = re.search(r"\bWHERE\b(.+)$", original_sql, re.I | re.S)
            if where_match:
                predicate = where_match.group(1).strip().rstrip(";")
                if ";" in predicate or "--" in predicate or "/*" in predicate:
                    raise ValueError(f"Unsafe partial-index predicate for {name}.")
                where_clause = " WHERE " + predicate
        columns_sql = ", ".join(quote_ident(col) for col in cols)
        lines.append(
            f"CREATE {unique}INDEX {qindex} ON {quote_ident(table_name)} "
            f"({columns_sql}){where_clause};"
        )


    # Add foreign keys after every table exists; this supports cyclic references.
    for counter, (table_name, fk_parts, resolved_target_columns) in enumerate(pending_fks, start=1):
        target_table = fk_parts[0]["table"]
        source_cols = ", ".join(quote_ident(part["from"]) for part in fk_parts)
        target_cols = ", ".join(quote_ident(column) for column in resolved_target_columns)
        raw_constraint = f"fk_{table_name}_{fk_parts[0]['from']}_{counter}"
        constraint = quote_ident(short_identifier(raw_constraint))
        action = str(fk_parts[0].get("on_delete") or "NO ACTION").upper()
        if action not in {"NO ACTION", "RESTRICT", "CASCADE", "SET NULL", "SET DEFAULT"}:
            action = "NO ACTION"
        update_action = str(fk_parts[0].get("on_update") or "NO ACTION").upper()
        if update_action not in {"NO ACTION", "RESTRICT", "CASCADE", "SET NULL", "SET DEFAULT"}:
            update_action = "NO ACTION"
        lines.append(
            f"ALTER TABLE {quote_ident(table_name)} ADD CONSTRAINT {constraint} "
            f"FOREIGN KEY ({source_cols}) REFERENCES {quote_ident(target_table)} "
            f"({target_cols}) ON UPDATE {update_action} ON DELETE {action};"
        )
    lines.append("")

    trigger_lines = generate_trigger_ddl(snapshot.get("triggers", []))
    if trigger_lines:
        lines.extend(["", *trigger_lines])
    # Bind every generated schema to an immutable installation fingerprint.
    # The marker is intentionally outside the source-table manifest so the
    # copy verifier can distinguish installer metadata from application data.
    baseline_body = "\n".join(lines).rstrip() + "\n"
    baseline_sha256 = hashlib.sha256(baseline_body.encode("utf-8")).hexdigest()
    lines.extend([
        "",
        "CREATE TABLE nexus_schema_installation (",
        "    id SMALLINT PRIMARY KEY CHECK (id = 1),",
        "    baseline_sha256 TEXT NOT NULL CHECK (baseline_sha256 ~ '^[0-9a-f]{64}
    for candidate in sorted(set(numeric_candidates)):
        candidate_table, candidate_column = candidate.split(".", 1)
        numeric_candidates_by_table.setdefault(candidate_table, []).append(candidate_column)
    numeric_review_candidates_by_table = {}
    for candidate in numeric_review_candidates:
        numeric_review_candidates_by_table.setdefault(candidate["table"], []).append({
            "column": candidate["column"],
            "source_type": candidate["source_type"],
            "postgres_type": candidate["postgres_type"],
            "reason": candidate["reason"],
        })

    foreign_key_manifest = []
    action_names = {
        "NO ACTION": "NO ACTION",
        "RESTRICT": "RESTRICT",
        "CASCADE": "CASCADE",
        "SET NULL": "SET NULL",
        "SET DEFAULT": "SET DEFAULT",
    }
    for table_name, fk_parts, resolved_target_columns in pending_fks:
        foreign_key_manifest.append({
            "table": table_name,
            "columns": [part["from"] for part in fk_parts],
            "referenced_table": fk_parts[0]["table"],
            "referenced_columns": list(resolved_target_columns),
            "on_update": action_names.get(
                str(fk_parts[0].get("on_update") or "NO ACTION").upper(), "NO ACTION"
            ),
            "on_delete": action_names.get(
                str(fk_parts[0].get("on_delete") or "NO ACTION").upper(), "NO ACTION"
            ),
        })
    foreign_key_manifest.sort(
        key=lambda item: (
            item["table"], tuple(item["columns"]),
            item["referenced_table"], tuple(item["referenced_columns"]),
        )
    )

    review = {
        "table_count": len(tables),
        "foreign_keys": foreign_key_manifest,
        "column_count": sum(len(table.get("columns", [])) for table in tables),
        "numeric_review_candidate_count": len(numeric_review_candidates),
        "numeric_review_candidates": numeric_review_candidates,
        "numeric_review_candidates_by_table": numeric_review_candidates_by_table,
        "text_numeric_candidate_count": len(set(numeric_candidates)),
        "text_numeric_candidates_by_table": numeric_candidates_by_table,
        "foreign_key_count": len(pending_fks),
        "index_count": len(pending_indexes),
        "trigger_count": len(snapshot.get("triggers", [])),
        "baseline_sha256": baseline_sha256,
        "installation_marker": "nexus_schema_installation",
        "identity_columns": identity_columns,
        "text_numeric_candidates": sorted(set(numeric_candidates)),
        "notes": [
            "Only explicitly reviewed accounting/rate columns use PostgreSQL NUMERIC; other TEXT fields remain TEXT.",
            "Numeric review candidates include matching SQLite TEXT and REAL columns; candidates are warnings, not automatic type conversions.",
            "SQLite REAL candidates require source-data and arithmetic review before selecting NUMERIC precision/scale.",
            "NUMERIC overrides are intentionally unconstrained until business precision/scale is approved.",
            "CHECK constraints are copied from the SQLite CREATE TABLE definitions.",
            "Expression indexes are copied from their SQLite CREATE INDEX definitions.",
            "SQLite COLLATE NOCASE unique columns are represented by unique lower(column) indexes.",
            "Validate generated DDL on the target PostgreSQL major version before production use.",
        ],
    }
    return "\n".join(lines), review


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="SQLite schema JSON snapshot")
    parser.add_argument("--output", required=True, help="Output PostgreSQL SQL file")
    parser.add_argument("--review-output", required=True, help="Output JSON review report")
    args = parser.parse_args(argv)
    snapshot = json.loads(Path(args.input).read_text(encoding="utf-8"))
    ddl, review = generate_postgres_ddl(snapshot)
    Path(args.output).write_text(ddl, encoding="utf-8")
    Path(args.review_output).write_text(
        json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "output": str(Path(args.output)),
        "review_output": str(Path(args.review_output)),
        "summary": {key: review[key] for key in ("table_count", "column_count", "foreign_key_count", "index_count", "trigger_count")},
        "numeric_candidates": len(review["text_numeric_candidates"]),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
),",
        "    installed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP",
        ");",
        "INSERT INTO nexus_schema_installation (id, baseline_sha256) VALUES (1, "
        + "'" + baseline_sha256 + "');",
        "",
        "COMMIT;",
        "",
    ])
    numeric_candidates_by_table = {}
    for candidate in sorted(set(numeric_candidates)):
        candidate_table, candidate_column = candidate.split(".", 1)
        numeric_candidates_by_table.setdefault(candidate_table, []).append(candidate_column)
    numeric_review_candidates_by_table = {}
    for candidate in numeric_review_candidates:
        numeric_review_candidates_by_table.setdefault(candidate["table"], []).append({
            "column": candidate["column"],
            "source_type": candidate["source_type"],
            "postgres_type": candidate["postgres_type"],
            "reason": candidate["reason"],
        })

    foreign_key_manifest = []
    action_names = {
        "NO ACTION": "NO ACTION",
        "RESTRICT": "RESTRICT",
        "CASCADE": "CASCADE",
        "SET NULL": "SET NULL",
        "SET DEFAULT": "SET DEFAULT",
    }
    for table_name, fk_parts, resolved_target_columns in pending_fks:
        foreign_key_manifest.append({
            "table": table_name,
            "columns": [part["from"] for part in fk_parts],
            "referenced_table": fk_parts[0]["table"],
            "referenced_columns": list(resolved_target_columns),
            "on_update": action_names.get(
                str(fk_parts[0].get("on_update") or "NO ACTION").upper(), "NO ACTION"
            ),
            "on_delete": action_names.get(
                str(fk_parts[0].get("on_delete") or "NO ACTION").upper(), "NO ACTION"
            ),
        })
    foreign_key_manifest.sort(
        key=lambda item: (
            item["table"], tuple(item["columns"]),
            item["referenced_table"], tuple(item["referenced_columns"]),
        )
    )

    review = {
        "table_count": len(tables),
        "foreign_keys": foreign_key_manifest,
        "column_count": sum(len(table.get("columns", [])) for table in tables),
        "numeric_review_candidate_count": len(numeric_review_candidates),
        "numeric_review_candidates": numeric_review_candidates,
        "numeric_review_candidates_by_table": numeric_review_candidates_by_table,
        "text_numeric_candidate_count": len(set(numeric_candidates)),
        "text_numeric_candidates_by_table": numeric_candidates_by_table,
        "foreign_key_count": len(pending_fks),
        "index_count": len(pending_indexes),
        "trigger_count": len(snapshot.get("triggers", [])),
        "identity_columns": identity_columns,
        "text_numeric_candidates": sorted(set(numeric_candidates)),
        "notes": [
            "Only explicitly reviewed accounting/rate columns use PostgreSQL NUMERIC; other TEXT fields remain TEXT.",
            "Numeric review candidates include matching SQLite TEXT and REAL columns; candidates are warnings, not automatic type conversions.",
            "SQLite REAL candidates require source-data and arithmetic review before selecting NUMERIC precision/scale.",
            "NUMERIC overrides are intentionally unconstrained until business precision/scale is approved.",
            "CHECK constraints are copied from the SQLite CREATE TABLE definitions.",
            "Expression indexes are copied from their SQLite CREATE INDEX definitions.",
            "SQLite COLLATE NOCASE unique columns are represented by unique lower(column) indexes.",
            "Validate generated DDL on the target PostgreSQL major version before production use.",
        ],
    }
    return "\n".join(lines), review


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="SQLite schema JSON snapshot")
    parser.add_argument("--output", required=True, help="Output PostgreSQL SQL file")
    parser.add_argument("--review-output", required=True, help="Output JSON review report")
    args = parser.parse_args(argv)
    snapshot = json.loads(Path(args.input).read_text(encoding="utf-8"))
    ddl, review = generate_postgres_ddl(snapshot)
    Path(args.output).write_text(ddl, encoding="utf-8")
    Path(args.review_output).write_text(
        json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "output": str(Path(args.output)),
        "review_output": str(Path(args.review_output)),
        "summary": {key: review[key] for key in ("table_count", "column_count", "foreign_key_count", "index_count", "trigger_count")},
        "numeric_candidates": len(review["text_numeric_candidates"]),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
