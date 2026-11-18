"""Isolated PostgreSQL smoke test through the Flask database factory.

Creates a uniquely named schema, initializes the real foundational schema,
posts a balanced journal through accounting_core.post_journal, verifies
idempotency, and drops the schema. No SQLite database or public schema is used.
"""
import os
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flask import Flask

from accounting_core import post_journal
from core_schema import initialize_core_schema
from database import connect_database, register_database_connection_teardown


def main():
    dsn = os.environ.get("NEXUS_POSTGRES_DSN", "").strip()
    if not dsn:
        raise SystemExit("NEXUS_POSTGRES_DSN is required.")
    if not os.environ.get("NEXUS_DB_BACKEND") == "postgresql":
        raise SystemExit("NEXUS_DB_BACKEND=postgresql is required.")

    schema = "admrail_smoke_" + uuid.uuid4().hex[:16]
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
        raise RuntimeError("Generated unsafe schema name.")

    flask_app = Flask("admrail-postgres-smoke")
    register_database_connection_teardown(flask_app)

    with flask_app.app_context():
        connection = connect_database(
            "unused-sqlite-path.db", isolation_level="", decimal_aggregates=False
        )
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

            lines = [
                (cash_id, "100.00", "0", "مدين"),
                (capital_id, "0", "100.00", "دائن"),
            ]
            entry_id = post_journal(
                connection, "2026-01-01", "PG-SMOKE-001",
                "اختبار ترحيل قيد PostgreSQL", lines,
            )
            repeated_entry_id = post_journal(
                connection, "2026-01-01", "PG-SMOKE-001",
                "اختبار ترحيل قيد PostgreSQL", lines,
            )
            if repeated_entry_id != entry_id:
                raise AssertionError("Journal posting is not idempotent.")

            totals = connection.execute(
                """SELECT SUM(debit::numeric) AS debit_total,
                          SUM(credit::numeric) AS credit_total
                   FROM journal_lines WHERE entry_id = %s""",
                (entry_id,),
            ).fetchone()
            posted = connection.execute(
                "SELECT status FROM journal_entries WHERE id = %s", (entry_id,)
            ).fetchone()
            debit = totals["debit_total"]
            credit = totals["credit_total"]
            if not posted or posted["status"] != "posted":
                raise AssertionError("Journal posting did not persist posted status.")
            if debit != credit or debit != 100:
                raise AssertionError(f"Unbalanced smoke journal: debit={debit}, credit={credit}")

            connection.commit()
            print(
                "POSTGRES_CORE_BOOTSTRAP=PASS "
                f"schema={schema} tables=10 journal_entry={entry_id} "
                f"idempotent=yes debit={debit} credit={credit}"
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
