"""Controlled 30-client FIFO-gate experiment for SQLite.

This is an isolated benchmark, not production integration. It models a single
Python process: all writer threads share one SQLiteWriteGate. The existing
baseline stress test remains unchanged. Every database is temporary.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import time
import unittest

from accounting_core import post_journal
from database import connect_database, enable_wal
from sqlite_write_gate import SQLiteWriteGate


def _prepare_gate_experiment_database(db_path):
    enable_wal(db_path)
    connection = connect_database(db_path)
    try:
        connection.executescript("""
            CREATE TABLE accounts(
                id INTEGER PRIMARY KEY, code TEXT NOT NULL, name_ar TEXT NOT NULL,
                account_type TEXT NOT NULL, parent_id INTEGER,
                is_group INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE journal_entries(
                id INTEGER PRIMARY KEY, entry_date TEXT NOT NULL, reference TEXT,
                description TEXT NOT NULL, status TEXT NOT NULL,
                created_by INTEGER, created_at TEXT NOT NULL
            );
            CREATE TABLE journal_lines(
                id INTEGER PRIMARY KEY, entry_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL, debit TEXT NOT NULL,
                credit TEXT NOT NULL, memo TEXT
            );
            INSERT INTO accounts(id,code,name_ar,account_type) VALUES
                (1,'1110','الصندوق','asset'),
                (2,'4100','الإيرادات','revenue');
        """)
        connection.execute("BEGIN")
        entries, lines = [], []
        historical_count = 5000
        for number in range(1, historical_count + 1):
            entries.append((
                number, "2026-01-15", f"GATE-HIST-{number:06d}",
                "قيد تاريخي لاختبار منسق الكتابة", "posted", 1,
                "2026-01-15 10:00:00",
            ))
            lines.extend([
                (number * 2 - 1, number, 1, "100.00", "0", "مدين"),
                (number * 2, number, 2, "0", "100.00", "دائن"),
            ])
        connection.executemany(
            "INSERT INTO journal_entries(id,entry_date,reference,description,status,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
            entries,
        )
        connection.executemany(
            "INSERT INTO journal_lines(id,entry_id,account_id,debit,credit,memo) VALUES(?,?,?,?,?,?)",
            lines,
        )
        connection.commit()
    finally:
        connection.close()


class SQLiteWriteGateLoadExperiment(unittest.TestCase):
    def test_thirty_clients_with_shared_fifo_gate(self):
        writer_count, reader_count = 15, 15
        client_count = writer_count + reader_count
        posts_per_writer, reads_per_reader = 20, 30
        historical_count = 5000
        streams = ("sales", "purchases", "inventory", "payroll")

        with tempfile.TemporaryDirectory(prefix="nexus-fifo-gate-load-") as folder:
            db_path = Path(folder) / "fifo-gate.sqlite3"
            _prepare_gate_experiment_database(db_path)
            gate = SQLiteWriteGate()
            start = threading.Barrier(client_count)
            metrics_lock = threading.Lock()
            write_latencies, gate_waits, transaction_work, report_latencies = [], [], [], []
            failures = []
            began = time.perf_counter()

            def writer(client_number):
                stream = streams[(client_number - 1) % len(streams)]
                start.wait(timeout=30)
                connection = connect_database(db_path, timeout=60, isolation_level="")
                try:
                    for sequence in range(posts_per_writer):
                        reference = f"GATE-{stream.upper()}-{client_number:02d}-{sequence:04d}"
                        tx_started = time.perf_counter()
                        acquired_at = None
                        waited = 0.0
                        try:
                            with gate.hold(timeout=60) as waited:
                                connection.execute("BEGIN IMMEDIATE")
                                acquired_at = time.perf_counter()
                                post_journal(
                                    connection, "2026-11-16", reference,
                                    f"اختبار ترحيل منسق - {stream}",
                                    [
                                        (1, "250.00", "0", f"مدين - {stream}"),
                                        (2, "0", "250.00", f"دائن - {stream}"),
                                    ],
                                    created_by=client_number,
                                )
                                connection.commit()
                        except Exception:
                            connection.rollback()
                            raise
                        finally:
                            finished = time.perf_counter()
                            with metrics_lock:
                                write_latencies.append(finished - tx_started)
                                gate_waits.append(waited)
                                if acquired_at is not None:
                                    transaction_work.append(finished - acquired_at)
                except Exception as exc:
                    with metrics_lock:
                        failures.append(("writer", client_number, repr(exc)))
                    raise
                finally:
                    connection.close()

            def report_reader(reader_number):
                start.wait(timeout=30)
                connection = connect_database(db_path, timeout=20, isolation_level=None)
                try:
                    for _ in range(reads_per_reader):
                        query_started = time.perf_counter()
                        try:
                            row = connection.execute("""
                                SELECT COUNT(DISTINCT je.id) AS entry_count,
                                       COUNT(jl.id) AS line_count,
                                       DECIMAL_SUM(jl.debit) AS debit_total,
                                       DECIMAL_SUM(jl.credit) AS credit_total
                                FROM journal_entries je
                                JOIN journal_lines jl ON jl.entry_id = je.id
                                WHERE je.entry_date BETWEEN ? AND ?
                            """, ("2026-01-01", "2026-12-31")).fetchone()
                            if row is None or row["entry_count"] < historical_count:
                                raise AssertionError("Report missed historical ledger rows")
                            if row["line_count"] < historical_count * 2:
                                raise AssertionError("Report missed historical journal lines")
                            if row["debit_total"] != row["credit_total"]:
                                raise AssertionError("Concurrent report found unbalanced totals")
                        except Exception as exc:
                            with metrics_lock:
                                failures.append(("reader", reader_number, repr(exc)))
                            raise
                        finally:
                            with metrics_lock:
                                report_latencies.append(time.perf_counter() - query_started)
                finally:
                    connection.close()

            with ThreadPoolExecutor(max_workers=client_count) as pool:
                futures = [pool.submit(writer, number) for number in range(1, writer_count + 1)]
                futures.extend(pool.submit(report_reader, number) for number in range(1, reader_count + 1))
                for future in futures:
                    future.result(timeout=240)

            elapsed = time.perf_counter() - began
            self.assertEqual(failures, [])
            self.assertEqual(len(write_latencies), writer_count * posts_per_writer)
            self.assertEqual(len(report_latencies), reader_count * reads_per_reader)

            ordered_writes = sorted(write_latencies)
            ordered_gate_waits = sorted(gate_waits)
            ordered_work = sorted(transaction_work)
            ordered_reports = sorted(report_latencies)
            write_p95 = ordered_writes[int(len(ordered_writes) * 0.95) - 1]
            gate_wait_p95 = ordered_gate_waits[int(len(ordered_gate_waits) * 0.95) - 1]
            work_p95 = ordered_work[int(len(ordered_work) * 0.95) - 1]
            report_p95 = ordered_reports[int(len(ordered_reports) * 0.95) - 1]
            print(
                "LAN_THIRTY_CLIENT_FIFO_GATE_METRICS "
                f"clients={client_count} writers={writer_count} readers={reader_count} "
                f"historical_entries={historical_count} new_entries={writer_count * posts_per_writer} "
                f"report_reads={len(report_latencies)} elapsed_seconds={elapsed:.3f} "
                f"write_p95_seconds={write_p95:.4f} write_max_seconds={max(ordered_writes):.4f} "
                f"gate_wait_p95_seconds={gate_wait_p95:.4f} "
                f"transaction_work_p95_seconds={work_p95:.4f} "
                f"report_p95_seconds={report_p95:.4f} report_max_seconds={max(ordered_reports):.4f}"
            )
            self.assertLess(elapsed, 150, "30-client FIFO-gate experiment exceeded 150 seconds")
            self.assertLess(write_p95, 5, "FIFO-gated journal write P95 exceeded five seconds")
            self.assertLess(max(ordered_writes), 60, "A FIFO-gated journal write exceeded 60 seconds")
            self.assertLess(report_p95, 5, "FIFO-gated report P95 exceeded five seconds")
            self.assertLess(max(ordered_reports), 15, "A FIFO-gated report read exceeded fifteen seconds")

            verify = connect_database(db_path)
            try:
                self.assertEqual(verify.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(
                    verify.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0],
                    historical_count + writer_count * posts_per_writer,
                )
                self.assertEqual(
                    verify.execute("SELECT COUNT(*) FROM journal_lines").fetchone()[0],
                    (historical_count + writer_count * posts_per_writer) * 2,
                )
                self.assertEqual(
                    verify.execute("""
                        SELECT COUNT(*) FROM (
                            SELECT entry_id, DECIMAL_SUM(debit) AS debit_total,
                                   DECIMAL_SUM(credit) AS credit_total
                            FROM journal_lines GROUP BY entry_id
                            HAVING debit_total <> credit_total
                        )
                    """).fetchone()[0],
                    0,
                )
            finally:
                verify.close()


if __name__ == "__main__":
    unittest.main()
