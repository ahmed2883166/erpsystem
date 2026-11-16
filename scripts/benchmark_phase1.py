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
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from accounting_core import reconciliation_snapshot
from database import connect_database
from management_dashboard import (
    build_management_snapshot,
    cached_management_snapshot,
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
            if entry_id == 1:
                description = "Synthetic opening capital"
                debit_account, credit_account = 1, 6
                debit_memo, credit_memo = "Opening cash", "Opening equity"
            elif entry_id % 2 == 0:
                description = "Synthetic cash sale"
                debit_account, credit_account = 1, 7
                debit_memo, credit_memo = "Cash sale", "Sales revenue"
            else:
                description = "Synthetic operating expense"
                debit_account, credit_account = 9, 1
                debit_memo, credit_memo = "Operating expense", "Cash payment"
            entry_rows.append((
                entry_id, entry_date.isoformat(), f"BENCH-{entry_id:07d}",
                description, "posted", "general", 1, "2026-11-12 00:00:00"
            ))
            line_id = entry_id * 2
            line_rows.append((line_id, entry_id, debit_account, amount, "0", debit_memo))
            line_rows.append((line_id + 1, entry_id, credit_account, "0", amount, credit_memo))
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
        dashboard_cold, dashboard_cold_seconds = timed(lambda: cached_management_snapshot(
            conn, date(2026, 11, 1), date(2026, 11, 30)
        ))
        dashboard_warm, dashboard_warm_seconds = timed(lambda: cached_management_snapshot(
            conn, date(2026, 11, 1), date(2026, 11, 30)
        ))
        reconciliation, reconciliation_seconds = timed(lambda: reconciliation_snapshot(conn))
        anomalies, anomalies_seconds = timed(lambda: unusual_journal_entries(
            conn, date(2026, 11, 1), date(2026, 11, 30)
        ))
        forecast, forecast_seconds = timed(lambda: cash_forecast(
            conn, date(2026, 11, 30), 8
        ))

        journal_query = "%Synthetic cash sale%"
        def journal_filter_benchmark():
            params = ("posted", "2026-11-01", "2026-11-30",
                      journal_query, journal_query, journal_query)
            conn.execute("""SELECT COUNT(*) FROM journal_entries
                WHERE status=? AND entry_date>=? AND entry_date<=?
                  AND (reference LIKE ? OR description LIKE ?)""",
                params[:5]).fetchone()
            return conn.execute("""SELECT id,entry_date,reference,description
                FROM journal_entries
                WHERE status=? AND entry_date>=? AND entry_date<=?
                  AND (reference LIKE ? OR description LIKE ?)
                ORDER BY entry_date DESC,id DESC LIMIT 50""",
                params[:5]).fetchall()
        journal_rows, journal_filter_seconds = timed(journal_filter_benchmark)

        def ledger_search_benchmark():
            like = "%Synthetic cash sale%"
            return conn.execute("""WITH ledger_rows AS (
                SELECT je.id entry_id,je.entry_date,je.reference,je.description,
                    jl.id line_id,jl.account_id,jl.debit,jl.credit,jl.memo,
                    DECIMAL_RUNNING_NET(jl.debit,jl.credit) OVER (
                        ORDER BY je.entry_date,jl.id
                        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                    ) running_balance
                FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
                WHERE jl.account_id=? AND je.status='posted'
                  AND je.entry_date>=? AND je.entry_date<=?
            )
            SELECT * FROM ledger_rows
            WHERE reference LIKE ? OR description LIKE ? OR memo LIKE ?
            ORDER BY entry_date,line_id LIMIT 50""",
            (1,"2026-11-01","2026-11-30",like,like,like)).fetchall()
        ledger_rows, ledger_search_seconds = timed(ledger_search_benchmark)

        result = {
            "database": "temporary synthetic SQLite database",
            "journal_entries": count,
            "journal_lines": count * 2,
            "results": {
                "management_snapshot_seconds": snapshot_seconds,
                "management_dashboard_cold_seconds": dashboard_cold_seconds,
                "management_dashboard_warm_seconds": dashboard_warm_seconds,
                "reconciliation_seconds": reconciliation_seconds,
                "unusual_journal_analysis_seconds": anomalies_seconds,
                "cash_forecast_seconds": forecast_seconds,
                "journal_filter_seconds": journal_filter_seconds,
                "ledger_search_seconds": ledger_search_seconds,
            },
            "checks": {
                "dashboard_revenue": str(snapshot["current"]["revenue"]),
                "cached_dashboard_has_forecast": "cash_forecast" in dashboard_cold,
                "cached_dashboard_has_anomaly_review": "unusual_entries" in dashboard_cold,
                "cached_dashboard_warm_revenue": str(dashboard_warm["current"]["revenue"]),
                "dashboard_net_profit": str(snapshot["current"]["net_profit"]),
                "reconciliation_debit": str(reconciliation["journal_totals"]["debit"]),
                "reconciliation_credit": str(reconciliation["journal_totals"]["credit"]),
                "unbalanced_entries": len(reconciliation["unbalanced"]),
                "flagged_entries": len(anomalies),
                "forecast_weeks": len(forecast["weeks"]),
                "journal_page_rows": len(journal_rows),
                "ledger_page_rows": len(ledger_rows),
                "balance_sheet_balanced": snapshot["balance_ok"],
            },
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        conn.close()


if __name__ == "__main__":
    main()
