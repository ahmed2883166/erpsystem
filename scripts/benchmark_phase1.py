"""Reproducible synthetic 100k-ledger benchmark for NEXUS ERP phase one.

Run: python scripts/benchmark_phase1.py --entries 100000
The benchmark creates an isolated temporary database and never touches company data.
"""
import argparse
import json
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

from accounting_core import reconciliation_snapshot
from database import connect_database
from management_dashboard import (
    build_management_snapshot,
    cash_forecast,
    initialize_management_dashboard_schema,
    unusual_journal_entries,
)


def timed(call):
    started = time.perf_counter()
    value = call()
    return value, round(time.perf_counter() - started, 3)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--entries", type=int, default=100000)
    args = parser.parse_args()
    count = max(1000, min(args.entries, 500000))

    with tempfile.TemporaryDirectory(prefix="nexus-phase1-benchmark-") as temp_dir:
        db_path = Path(temp_dir) / "benchmark.sqlite3"
        conn = connect_database(db_path)
        conn.executescript("""
            CREATE TABLE accounts(
                id INTEGER PRIMARY KEY,code TEXT,name_ar TEXT,account_type TEXT,
                is_group INTEGER DEFAULT 0,active INTEGER DEFAULT 1
            );
            CREATE TABLE journal_entries(
                id INTEGER PRIMARY KEY,entry_date TEXT,reference TEXT,description TEXT,
                status TEXT,entry_type TEXT DEFAULT 'general',created_by INTEGER,created_at TEXT
            );
            CREATE TABLE journal_lines(
                id INTEGER PRIMARY KEY,entry_id INTEGER,account_id INTEGER,
                debit TEXT DEFAULT '0',credit TEXT DEFAULT '0',memo TEXT DEFAULT ''
            );
            CREATE TABLE company_settings(
                setting_key TEXT PRIMARY KEY,setting_value TEXT
            );
            INSERT INTO company_settings VALUES('activity','commercial');
            INSERT INTO company_settings VALUES('base_currency','EGP');
            INSERT INTO company_settings VALUES('company_name','NEXUS Synthetic Benchmark');
            INSERT INTO accounts(id,code,name_ar,account_type) VALUES
                (1,'1110','الصندوق','asset'),
                (2,'1120','البنوك','asset'),
                (3,'1130','العملاء','asset'),
                (4,'1150','المخزون','asset'),
                (5,'2110','الموردون','liability'),
                (6,'3100','رأس المال','equity'),
                (7,'4100','المبيعات','revenue'),
                (8,'5100','تكلفة المبيعات','expense'),
                (9,'6400','مصروفات إدارية','expense');
        """)

        entry_rows, line_rows = [], []
        historical_count = max(1, count // 5)
        for entry_id in range(1, count + 1):
            if entry_id <= historical_count:
                entry_date = date(2026, 8, 1) + timedelta(days=(entry_id % 92))
            else:
                entry_date = date(2026, 11, 1) + timedelta(days=(entry_id % 30))
            amount = str(100 + (entry_id % 17))
            entry_rows.append((
                entry_id, entry_date.isoformat(), f"BENCH-{entry_id:07d}",
                "Synthetic operating expense", "posted", "general", 1, "2026-11-12 00:00:00"
            ))
            line_id = entry_id * 2
            line_rows.append((line_id, entry_id, 9, amount, "0", "Operating expense"))
            line_rows.append((line_id + 1, entry_id, 1, "0", amount, "Cash payment"))
        conn.executemany(
            "INSERT INTO journal_entries VALUES(?,?,?,?,?,?,?,?)", entry_rows
        )
        conn.executemany(
            "INSERT INTO journal_lines VALUES(?,?,?,?,?,?)", line_rows
        )
        initialize_management_dashboard_schema(conn, "commercial")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_benchmark_journal_date_id ON journal_entries(entry_date,id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_benchmark_lines_account_entry ON journal_lines(account_id,entry_id)")
        conn.execute("ANALYZE")
        conn.commit()

        snapshot, snapshot_seconds = timed(lambda: build_management_snapshot(
            conn, date(2026, 11, 1), date(2026, 11, 30)
        ))
        reconciliation, reconciliation_seconds = timed(lambda: reconciliation_snapshot(conn))
        anomalies, anomalies_seconds = timed(lambda: unusual_journal_entries(
            conn, date(2026, 11, 1), date(2026, 11, 30)
        ))
        forecast, forecast_seconds = timed(lambda: cash_forecast(
            conn, date(2026, 11, 30), 8
        ))

        result = {
            "database": "temporary synthetic SQLite database",
            "journal_entries": count,
            "journal_lines": count * 2,
            "results": {
                "management_snapshot_seconds": snapshot_seconds,
                "reconciliation_seconds": reconciliation_seconds,
                "unusual_journal_analysis_seconds": anomalies_seconds,
                "cash_forecast_seconds": forecast_seconds,
            },
            "checks": {
                "dashboard_revenue": str(snapshot["current"]["revenue"]),
                "dashboard_net_profit": str(snapshot["current"]["net_profit"]),
                "reconciliation_debit": str(reconciliation["journal_totals"]["debit"]),
                "reconciliation_credit": str(reconciliation["journal_totals"]["credit"]),
                "unbalanced_entries": len(reconciliation["unbalanced"]),
                "flagged_entries": len(anomalies),
                "forecast_weeks": len(forecast["weeks"]),
            },
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        conn.close()


if __name__ == "__main__":
    main()
