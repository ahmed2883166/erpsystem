"""SQLite/PostgreSQL parity tests for the indirect cash-flow report."""
import os
import sqlite3
import unittest
import uuid
from decimal import Decimal
from unittest.mock import patch

from accounting_core import register_decimal_aggregates
from financial_planning import (
    build_cash_flow_indirect_snapshot,
    build_cash_flow_snapshot,
)

POSTGRES_DSN = os.environ.get("NEXUS_TEST_POSTGRES_DSN", "").strip()


def _seed_cash_flow_fixture(connection, placeholder, *, sqlite=False):
    connection.execute("""
        CREATE TABLE accounts(
            id BIGINT PRIMARY KEY, code TEXT, name_ar TEXT,
            account_type TEXT, is_group INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1, parent_id BIGINT,
            statement_section TEXT
        )
    """)
    connection.execute("""
        CREATE TABLE journal_entries(
            id BIGINT PRIMARY KEY, entry_date TEXT, status TEXT,
            reference TEXT, entry_type TEXT
        )
    """)
    amount_type = "TEXT" if sqlite else "NUMERIC(24,6)"
    connection.execute(f"""
        CREATE TABLE journal_lines(
            id BIGINT PRIMARY KEY, entry_id BIGINT, account_id BIGINT,
            debit {amount_type}, credit {amount_type}
        )
    """)
    connection.execute("""
        CREATE TABLE management_dashboard_meta(
            meta_key TEXT PRIMARY KEY, meta_value TEXT
        )
    """)
    connection.execute("""
        CREATE TABLE cash_flow_account_mappings(
            account_id BIGINT PRIMARY KEY, category TEXT NOT NULL
        )
    """)
    accounts = [
        (1, "1110", "الصندوق", "asset", 0, 1, None, "current_assets"),
        (2, "1150", "العملاء", "asset", 0, 1, None, "current_assets"),
        (3, "2110", "الموردون", "liability", 0, 1, None, "current_liabilities"),
        (4, "3100", "رأس المال", "equity", 0, 1, None, "equity"),
        (5, "4100", "إيرادات المبيعات", "revenue", 0, 1, None, "revenue"),
        (6, "6100", "مصروفات التشغيل", "expense", 0, 1, None, "operating_expenses"),
    ]
    account_sql = "INSERT INTO accounts VALUES(" + ",".join([placeholder] * 8) + ")"
    if sqlite:
        connection.executemany(account_sql, accounts)
    else:
        connection.cursor().executemany(account_sql, accounts)

    entries = [
        (1, "2026-10-31", "posted", "OPENING-2026", "general"),
        (2, "2026-11-05", "posted", "SALE-001", "general"),
        (3, "2026-11-08", "posted", "RECEIPT-001", "general"),
        (4, "2026-11-12", "posted", "EXPENSE-001", "general"),
    ]
    entry_sql = "INSERT INTO journal_entries VALUES(" + ",".join([placeholder] * 5) + ")"
    if sqlite:
        connection.executemany(entry_sql, entries)
    else:
        connection.cursor().executemany(entry_sql, entries)

    lines = [
        (1, 1, 1, "1000", "0"),
        (2, 1, 4, "0", "1000"),
        (3, 1, 2, "200", "0"),
        (4, 1, 4, "0", "200"),
        (5, 2, 2, "300", "0"),
        (6, 2, 5, "0", "300"),
        (7, 3, 1, "100", "0"),
        (8, 3, 2, "0", "100"),
        (9, 4, 6, "50", "0"),
        (10, 4, 1, "0", "30"),
        (11, 4, 3, "0", "20"),
    ]
    line_sql = "INSERT INTO journal_lines VALUES(" + ",".join([placeholder] * 5) + ")"
    if sqlite:
        connection.executemany(line_sql, lines)
    else:
        connection.cursor().executemany(line_sql, lines)


def _normalized_cash_flow(snapshot):
    return {
        "opening": snapshot["opening"],
        "closing": snapshot["closing"],
        "net_change": snapshot["net_change"],
        "net_profit": snapshot["net_profit"],
        "sections": {
            name: {"in": values["in"], "out": values["out"]}
            for name, values in snapshot["sections"].items()
        },
        "indirect_operating_net": snapshot["indirect_operating_net"],
        "indirect_reconciliation": snapshot["indirect_reconciliation"],
        "reconciliation_difference": snapshot["reconciliation_difference"],
        "indirect_items": [
            (item["label"], item["amount"], item["kind"])
            for item in snapshot["indirect_items"]
        ],
    }


@unittest.skipUnless(POSTGRES_DSN, "NEXUS_TEST_POSTGRES_DSN is not configured")
class CashFlowPostgreSQLParityTests(unittest.TestCase):
    def test_sqlite_and_postgresql_indirect_cash_flow_match_same_fixture(self):
        import psycopg
        from psycopg.rows import dict_row

        sqlite_connection = sqlite3.connect(":memory:")
        sqlite_connection.row_factory = sqlite3.Row
        register_decimal_aggregates(sqlite_connection)
        schema = "nexus_cash_flow_parity_" + uuid.uuid4().hex[:12]
        pg_connection = psycopg.connect(POSTGRES_DSN, row_factory=dict_row)
        roles = {
            "cash": {1},
            "receivables": {2},
            "inventory": set(),
            "payables": {3},
            "current_assets": {1, 2},
            "current_liabilities": {3},
        }

        try:
            _seed_cash_flow_fixture(sqlite_connection, "?", sqlite=True)
            sqlite_connection.commit()
            with pg_connection.transaction():
                pg_connection.execute(f'CREATE SCHEMA "{schema}"')
                pg_connection.execute(f'SET LOCAL search_path TO "{schema}"')
                _seed_cash_flow_fixture(pg_connection, "%s", sqlite=False)

                with patch("management_dashboard.load_management_account_roles", return_value=roles), \
                     patch("management_dashboard._copied_opening_entry_ids", return_value=set()):
                    sqlite_direct = build_cash_flow_snapshot(
                        sqlite_connection, "2026-11-01", "2026-11-30"
                    )
                    pg_direct = build_cash_flow_snapshot(
                        pg_connection, "2026-11-01", "2026-11-30"
                    )
                    self.assertEqual(pg_direct["opening"], sqlite_direct["opening"])
                    self.assertEqual(pg_direct["closing"], sqlite_direct["closing"])
                    self.assertEqual(pg_direct["sections"], sqlite_direct["sections"])

                    sqlite_report = build_cash_flow_indirect_snapshot(
                        sqlite_connection, "2026-11-01", "2026-11-30"
                    )
                    pg_report = build_cash_flow_indirect_snapshot(
                        pg_connection, "2026-11-01", "2026-11-30"
                    )

                self.assertEqual(
                    _normalized_cash_flow(pg_report),
                    _normalized_cash_flow(sqlite_report),
                )
                self.assertEqual(pg_report["net_profit"], Decimal("250"))
                self.assertEqual(pg_report["indirect_operating_net"], Decimal("70"))
                self.assertEqual(pg_report["indirect_reconciliation"], Decimal("0"))
                self.assertEqual(pg_report["reconciliation_difference"], Decimal("0"))
                self.assertEqual(pg_report["sections"]["operating"]["in"], Decimal("70"))
                self.assertEqual(pg_report["sections"]["operating"]["out"], Decimal("0"))
                raise RuntimeError("rollback disposable schema")
        except RuntimeError as exc:
            self.assertEqual(str(exc), "rollback disposable schema")
        finally:
            sqlite_connection.close()
            pg_connection.close()


if __name__ == "__main__":
    unittest.main()
