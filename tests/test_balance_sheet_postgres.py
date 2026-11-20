"""SQLite/PostgreSQL parity tests for the balance-sheet snapshot."""
import os
import sqlite3
import unittest
import uuid
from decimal import Decimal

from accounting_core import register_decimal_aggregates
from financial_planning import build_balance_sheet_snapshot

POSTGRES_DSN = os.environ.get("NEXUS_TEST_POSTGRES_DSN", "").strip()


def _seed_balance_sheet_fixture(connection, placeholder, *, sqlite=False):
    connection.execute("""
        CREATE TABLE accounts(
            id BIGINT PRIMARY KEY, code TEXT, name_ar TEXT,
            account_type TEXT, parent_id BIGINT,
            is_group BOOLEAN DEFAULT FALSE,
            statement_section TEXT, account_subtype TEXT
        )
    """)
    connection.execute("""
        CREATE TABLE journal_entries(
            id BIGINT PRIMARY KEY, entry_date TEXT, status TEXT
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
        CREATE TABLE financial_years(
            id BIGINT PRIMARY KEY, year_name TEXT, start_date TEXT,
            end_date TEXT, status TEXT, opening_entry_id BIGINT,
            closing_entry_id BIGINT
        )
    """)
    connection.execute("""
        CREATE TABLE financial_year_journal_history(
            id BIGINT PRIMARY KEY, financial_year_id BIGINT,
            entry_kind TEXT, action TEXT, entry_id BIGINT,
            related_entry_id BIGINT, ledger_history_copied INTEGER
        )
    """)

    accounts = [
        (1, "1110", "النقدية", "asset", None, False, "assets", None),
        (2, "2110", "الموردون", "liability", None, False, "liabilities", None),
        (3, "3100", "رأس المال", "equity", None, False, "equity", None),
        (4, "4100", "إيرادات المبيعات", "revenue", None, False, "revenue", None),
        (5, "6100", "مصروفات التشغيل", "expense", None, False, "operating_expenses", None),
    ]
    insert_accounts = "INSERT INTO accounts VALUES(" + ",".join([placeholder] * 8) + ")"
    if sqlite:
        connection.executemany(insert_accounts, accounts)
    else:
        connection.cursor().executemany(insert_accounts, accounts)

    connection.execute(
        "INSERT INTO financial_years VALUES(1,'FY2026','2026-01-01','2026-12-31','closed',NULL,NULL)"
    )
    connection.execute(
        "INSERT INTO financial_years VALUES(2,'FY2027','2027-01-01','2027-12-31','open',2,NULL)"
    )
    connection.execute(
        "INSERT INTO financial_year_journal_history "
        "VALUES(1,2,'opening','created',2,NULL,1)"
    )

    entries = [
        (1, "2026-12-31", "posted"),
        (2, "2027-01-01", "posted"),  # copied opening balance; must be excluded
        (3, "2027-01-10", "posted"),
        (4, "2027-01-12", "posted"),
        (5, "2027-01-15", "draft"),   # draft must not affect the report
    ]
    entry_sql = "INSERT INTO journal_entries VALUES(" + ",".join([placeholder] * 3) + ")"
    if sqlite:
        connection.executemany(entry_sql, entries)
    else:
        connection.cursor().executemany(entry_sql, entries)

    lines = [
        (1, 1, 1, "515517.40", "0"),
        (2, 1, 3, "0", "515517.40"),
        (3, 2, 1, "515517.40", "0"),
        (4, 2, 3, "0", "515517.40"),
        (5, 3, 1, "100", "0"),
        (6, 3, 4, "0", "100"),
        (7, 4, 5, "20", "0"),
        (8, 4, 2, "0", "20"),
        (9, 5, 1, "999", "0"),
        (10, 5, 4, "0", "999"),
    ]
    line_sql = "INSERT INTO journal_lines VALUES(" + ",".join([placeholder] * 5) + ")"
    if sqlite:
        connection.executemany(line_sql, lines)
    else:
        connection.cursor().executemany(line_sql, lines)


def _normalized_balance_sheet(snapshot):
    sections = {}
    for section, rows in snapshot["sections"].items():
        sections[section] = [
            (
                row["code"], row["name_ar"], row["account_type"],
                row["balance"], row["display_balance"], row["depth"],
                row["statement_section"],
            )
            for row in rows
        ]
    return {
        "sections": sections,
        "total_assets": snapshot["total_assets"],
        "total_liabilities": snapshot["total_liabilities"],
        "equity_before_profit": snapshot["equity_before_profit"],
        "net_profit": snapshot["net_profit"],
        "total_equity": snapshot["total_equity"],
        "balance_difference": snapshot["balance_difference"],
        "balanced": snapshot["balanced"],
        "negative_asset_accounts": snapshot["negative_asset_accounts"],
        "opening_balance_unclassified_count": snapshot["opening_balance_unclassified_count"],
        "opening_balance_classification_conflict_count": snapshot["opening_balance_classification_conflict_count"],
        "opening_balance_review_count": snapshot["opening_balance_review_count"],
    }


@unittest.skipUnless(POSTGRES_DSN, "NEXUS_TEST_POSTGRES_DSN is not configured")
class BalanceSheetPostgreSQLParityTests(unittest.TestCase):
    def test_sqlite_and_postgresql_balance_sheets_match_and_exclude_copied_opening(self):
        import psycopg
        from psycopg.rows import dict_row

        sqlite_connection = sqlite3.connect(":memory:")
        sqlite_connection.row_factory = sqlite3.Row
        register_decimal_aggregates(sqlite_connection)
        schema = "nexus_balance_sheet_parity_" + uuid.uuid4().hex[:12]
        pg_connection = psycopg.connect(POSTGRES_DSN, row_factory=dict_row)

        try:
            _seed_balance_sheet_fixture(sqlite_connection, "?", sqlite=True)
            sqlite_snapshot = build_balance_sheet_snapshot(
                sqlite_connection, "2027-01-31"
            )

            with pg_connection.transaction():
                pg_connection.execute(f'CREATE SCHEMA "{schema}"')
                pg_connection.execute(f'SET LOCAL search_path TO "{schema}"')
                _seed_balance_sheet_fixture(pg_connection, "%s", sqlite=False)
                pg_snapshot = build_balance_sheet_snapshot(
                    pg_connection, "2027-01-31"
                )

                self.assertEqual(
                    _normalized_balance_sheet(pg_snapshot),
                    _normalized_balance_sheet(sqlite_snapshot),
                )
                self.assertEqual(pg_snapshot["total_assets"], Decimal("515597.40"))
                self.assertEqual(pg_snapshot["total_liabilities"], Decimal("20"))
                self.assertEqual(pg_snapshot["equity_before_profit"], Decimal("515517.40"))
                self.assertEqual(pg_snapshot["net_profit"], Decimal("80"))
                self.assertEqual(pg_snapshot["total_equity"], Decimal("515597.40"))
                self.assertEqual(pg_snapshot["balance_difference"], Decimal("0.00"))
                self.assertTrue(pg_snapshot["balanced"])
                self.assertEqual(pg_snapshot["opening_balance_review_count"], 0)
                raise RuntimeError("rollback disposable schema")
        except RuntimeError as exc:
            self.assertEqual(str(exc), "rollback disposable schema")
        finally:
            sqlite_connection.close()
            pg_connection.close()


if __name__ == "__main__":
    unittest.main()
