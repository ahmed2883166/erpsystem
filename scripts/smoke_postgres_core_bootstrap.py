"""Isolated PostgreSQL smoke test for the real ERP core schema.

This script creates a uniquely named temporary schema, initializes the same
core schema used by the application, exercises a balanced journal, and drops
the schema. It never touches SQLite files or the public PostgreSQL schema.
Run with NEXUS_POSTGRES_DSN set.
"""
import os
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from postgres_database import connect_postgres
from core_schema import initialize_core_schema


def main():
    dsn = os.environ.get("NEXUS_POSTGRES_DSN", "").strip()
    if not dsn:
        raise SystemExit("NEXUS_POSTGRES_DSN is required.")

    schema = "admrail_smoke_" + uuid.uuid4().hex[:16]
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
        raise RuntimeError("Generated unsafe schema name.")

    connection = connect_postgres(dsn, application_name="ADMRAIL PostgreSQL core smoke")
    created = False
    try:
        connection.execute(f'CREATE SCHEMA "{schema}"')
        created = True
        connection.execute(f'SET search_path TO "{schema}"')
        initialize_core_schema(connection)

        connection.execute(
            "INSERT INTO company_settings(setting_key, setting_value) VALUES (%s, %s)",
            ("company_name", "ADMRAIL PostgreSQL Smoke"),
        )
        cash_id = connection.execute(
            """INSERT INTO accounts(code, name_ar, account_type, is_group, active)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            ("1000", "الصندوق - اختبار", "asset", 0, 1),
        ).fetchone()["id"]
        capital_id = connection.execute(
            """INSERT INTO accounts(code, name_ar, account_type, is_group, active)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            ("3000", "رأس المال - اختبار", "equity", 0, 1),
        ).fetchone()["id"]
        entry_id = connection.execute(
            """INSERT INTO journal_entries(entry_date, reference, description, status, created_at)
               VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP::text) RETURNING id""",
            ("2026-01-01", "PG-SMOKE-001", "اختبار قيد PostgreSQL", "posted"),
        ).fetchone()["id"]
        connection.execute(
            """INSERT INTO journal_lines(entry_id, account_id, debit, credit, memo)
               VALUES (%s, %s, %s, %s, %s), (%s, %s, %s, %s, %s)""",
            (entry_id, cash_id, "100.00", "0", "مدين",
             entry_id, capital_id, "0", "100.00", "دائن"),
        )
        totals = connection.execute(
            """SELECT SUM(debit::numeric) AS debit_total,
                      SUM(credit::numeric) AS credit_total
               FROM journal_lines WHERE entry_id = %s""",
            (entry_id,),
        ).fetchone()
        debit = totals["debit_total"]
        credit = totals["credit_total"]
        if debit != credit or debit != 100:
            raise AssertionError(f"Unbalanced smoke journal: debit={debit}, credit={credit}")
        connection.commit()
        print(
            "POSTGRES_CORE_BOOTSTRAP=PASS "
            f"schema={schema} tables=10 journal_entry={entry_id} "
            f"debit={debit} credit={credit}"
        )
        return 0
    except Exception:
        connection.rollback()
        raise
    finally:
        if created:
            try:
                connection.execute("SET search_path TO public")
                connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                connection.commit()
            except Exception:
                connection.rollback()
        connection.close()


if __name__ == "__main__":
    sys.exit(main())
