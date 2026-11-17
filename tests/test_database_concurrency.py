"""Ten-client SQLite write-concurrency regression tests.

These exercise the same connection factory and journal posting function used by
the ERP, against isolated temporary files. They never open the company database.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest

from accounting_core import post_journal
from database import connect_database, enable_wal


def prepare_database(db_path):
    enable_wal(db_path)
    setup = connect_database(db_path)
    try:
        setup.executescript("""
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
        setup.commit()
    finally:
        setup.close()


class TenClientWriteConcurrencyTests(unittest.TestCase):
    CLIENTS = 10
    POSTS_PER_CLIENT = 25

    def test_ten_clients_can_post_journals_without_lost_or_duplicate_writes(self):
        with tempfile.TemporaryDirectory(prefix="nexus-ten-client-") as folder:
            db_path = Path(folder) / "concurrency.sqlite3"
            prepare_database(db_path)
            start = threading.Barrier(self.CLIENTS)

            def client_work(client_number):
                start.wait(timeout=15)
                posted = []
                connection = connect_database(db_path, timeout=20, isolation_level="")
                try:
                    for sequence in range(self.POSTS_PER_CLIENT):
                        reference = f"LAN-{client_number:02d}-{sequence:04d}"
                        connection.execute("BEGIN IMMEDIATE")
                        try:
                            entry_id = post_journal(
                                connection, "2026-11-16", reference,
                                "اختبار تزامن شبكة محلية",
                                [
                                    (1, "125.50", "0", "مدين الصندوق"),
                                    (2, "0", "125.50", "دائن الإيراد"),
                                ],
                                created_by=client_number,
                            )
                            connection.commit()
                            posted.append(entry_id)
                        except Exception:
                            connection.rollback()
                            raise
                    return posted
                finally:
                    connection.close()

            with ThreadPoolExecutor(max_workers=self.CLIENTS) as pool:
                futures = [pool.submit(client_work, number) for number in range(1, self.CLIENTS + 1)]
                results = [future.result(timeout=90) for future in futures]

            expected_entries = self.CLIENTS * self.POSTS_PER_CLIENT
            self.assertEqual(sum(len(items) for items in results), expected_entries)
            self.assertEqual(len({entry_id for items in results for entry_id in items}), expected_entries)

            verify = connect_database(db_path)
            try:
                self.assertEqual(verify.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(verify.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0], expected_entries)
                self.assertEqual(verify.execute("SELECT COUNT(*) FROM journal_lines").fetchone()[0], expected_entries * 2)
                self.assertEqual(verify.execute("""
                    SELECT COUNT(*) FROM (
                        SELECT reference FROM journal_entries
                        GROUP BY reference HAVING COUNT(*) <> 1
                    )
                """).fetchone()[0], 0)
                self.assertEqual(verify.execute("""
                    SELECT COUNT(*) FROM (
                        SELECT entry_id, DECIMAL_SUM(debit) AS debit_total,
                            DECIMAL_SUM(credit) AS credit_total
                        FROM journal_lines GROUP BY entry_id
                        HAVING debit_total <> credit_total
                    )
                """).fetchone()[0], 0)
            finally:
                verify.close()

    def test_ten_clients_posting_the_same_reference_create_only_one_journal(self):
        with tempfile.TemporaryDirectory(prefix="nexus-ten-client-idempotency-") as folder:
            db_path = Path(folder) / "idempotency.sqlite3"
            prepare_database(db_path)
            start = threading.Barrier(self.CLIENTS)

            def client_work(client_number):
                start.wait(timeout=15)
                connection = connect_database(db_path, timeout=20, isolation_level="")
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        entry_id = post_journal(
                            connection, "2026-11-16", "LAN-SAME-REFERENCE",
                            "اختبار منع تكرار القيد",
                            [
                                (1, "250.00", "0", "مدين الصندوق"),
                                (2, "0", "250.00", "دائن الإيراد"),
                            ],
                            created_by=client_number,
                        )
                        connection.commit()
                        return entry_id
                    except Exception:
                        connection.rollback()
                        raise
                finally:
                    connection.close()

            with ThreadPoolExecutor(max_workers=self.CLIENTS) as pool:
                futures = [pool.submit(client_work, number) for number in range(1, self.CLIENTS + 1)]
                entry_ids = [future.result(timeout=90) for future in futures]

            self.assertEqual(len(set(entry_ids)), 1)
            verify = connect_database(db_path)
            try:
                self.assertEqual(verify.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0], 1)
                self.assertEqual(verify.execute("SELECT COUNT(*) FROM journal_lines").fetchone()[0], 2)
            finally:
                verify.close()


    def test_ten_writers_and_five_report_readers_run_together(self):
        """Exercise WAL read/write overlap with isolated ERP journal data."""
        import time

        with tempfile.TemporaryDirectory(prefix="nexus-lan-read-write-load-") as folder:
            db_path = Path(folder) / "mixed-load.sqlite3"
            prepare_database(db_path)
            worker_count = 15
            start = threading.Barrier(worker_count)
            read_latencies = []
            read_errors = []
            expected_entries = self.CLIENTS * self.POSTS_PER_CLIENT

            def writer(client_number):
                start.wait(timeout=20)
                connection = connect_database(db_path, timeout=20, isolation_level="")
                try:
                    for sequence in range(self.POSTS_PER_CLIENT):
                        reference = f"MIXED-{client_number:02d}-{sequence:04d}"
                        connection.execute("BEGIN IMMEDIATE")
                        try:
                            post_journal(
                                connection, "2026-11-16", reference,
                                "اختبار حمل قراءة وكتابة متزامن",
                                [
                                    (1, "75.25", "0", "مدين الصندوق"),
                                    (2, "0", "75.25", "دائن الإيراد"),
                                ],
                                created_by=client_number,
                            )
                            connection.commit()
                        except Exception:
                            connection.rollback()
                            raise
                finally:
                    connection.close()

            def report_reader(reader_number):
                start.wait(timeout=20)
                connection = connect_database(db_path, timeout=20, isolation_level=None)
                try:
                    for _ in range(40):
                        began = time.perf_counter()
                        try:
                            row = connection.execute("""
                                SELECT COUNT(*) AS entries,
                                       COALESCE(SUM((SELECT COUNT(*) FROM journal_lines jl
                                                     WHERE jl.entry_id=je.id)), 0) AS lines
                                FROM journal_entries je
                            """).fetchone()
                            self.assertGreaterEqual(row["entries"], 0)
                            self.assertGreaterEqual(row["lines"], 0)
                        except Exception as exc:
                            read_errors.append((reader_number, repr(exc)))
                            raise
                        finally:
                            read_latencies.append(time.perf_counter() - began)
                finally:
                    connection.close()

            began = time.perf_counter()
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                futures = [
                    pool.submit(writer, number)
                    for number in range(1, self.CLIENTS + 1)
                ]
                futures.extend(
                    pool.submit(report_reader, number)
                    for number in range(1, 6)
                )
                for future in futures:
                    future.result(timeout=90)
            elapsed = time.perf_counter() - began

            self.assertEqual(read_errors, [])
            self.assertEqual(len(read_latencies), 200)
            ordered = sorted(read_latencies)
            p95 = ordered[int(len(ordered) * 0.95) - 1]
            print(
                "LAN_MIXED_LOAD_METRICS "
                f"writers={self.CLIENTS} readers=5 entries={expected_entries} "
                f"reads={len(read_latencies)} elapsed_seconds={elapsed:.3f} "
                f"read_p95_seconds={p95:.4f} read_max_seconds={max(ordered):.4f}"
            )
            self.assertLess(elapsed, 60, "Mixed ten-client write and report-read workload took too long")
            self.assertLess(p95, 5, "95th percentile report-read latency exceeded five seconds")
            self.assertLess(max(ordered), 10, "A report read took more than ten seconds")

            verify = connect_database(db_path)
            try:
                self.assertEqual(verify.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(
                    verify.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0],
                    expected_entries,
                )
                self.assertEqual(
                    verify.execute("SELECT COUNT(*) FROM journal_lines").fetchone()[0],
                    expected_entries * 2,
                )
                self.assertEqual(
                    verify.execute("""
                        SELECT COUNT(*) FROM (
                            SELECT entry_id,
                                   DECIMAL_SUM(debit) AS debit_total,
                                   DECIMAL_SUM(credit) AS credit_total
                            FROM journal_lines GROUP BY entry_id
                            HAVING debit_total <> credit_total
                        )
                    """).fetchone()[0],
                    0,
                )
            finally:
                verify.close()


    def test_twenty_clients_mix_journal_entry_and_financial_report_reads(self):
        """Stress 20 simultaneous clients against a larger isolated SQLite ledger."""
        import time

        writer_count = 12
        reader_count = 8
        clients = writer_count + reader_count
        posts_per_writer = 20
        reads_per_reader = 30

        with tempfile.TemporaryDirectory(prefix="nexus-twenty-client-mixed-load-") as folder:
            db_path = Path(folder) / "twenty-client.sqlite3"
            prepare_database(db_path)
            setup = connect_database(db_path)
            try:
                setup.executescript("""
                    CREATE INDEX idx_journal_entries_date ON journal_entries(entry_date);
                    CREATE INDEX idx_journal_entries_reference ON journal_entries(reference);
                    CREATE INDEX idx_journal_lines_entry_account ON journal_lines(entry_id, account_id);
                """)
                # A pre-existing ledger makes report reads more representative than
                # measuring an almost-empty database.
                setup.execute("BEGIN")
                entries = []
                lines = []
                for number in range(1, 5001):
                    entries.append((
                        number, "2026-01-15", f"HIST-{number:06d}",
                        "رصيد تاريخي لاختبار التقارير", "posted", 1,
                        "2026-01-15 10:00:00",
                    ))
                    lines.extend([
                        (number * 2 - 1, number, 1, "100.00", "0", "مدين"),
                        (number * 2, number, 2, "0", "100.00", "دائن"),
                    ])
                setup.executemany(
                    "INSERT INTO journal_entries(id,entry_date,reference,description,status,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    entries,
                )
                setup.executemany(
                    "INSERT INTO journal_lines(id,entry_id,account_id,debit,credit,memo) VALUES(?,?,?,?,?,?)",
                    lines,
                )
                setup.commit()
            finally:
                setup.close()

            start = threading.Barrier(clients)
            writer_latencies = []
            reader_latencies = []
            latency_lock = threading.Lock()
            failures = []
            began = time.perf_counter()

            def writer(client_number):
                start.wait(timeout=20)
                connection = connect_database(db_path, timeout=20, isolation_level="")
                try:
                    for sequence in range(posts_per_writer):
                        reference = f"TWENTY-{client_number:02d}-{sequence:04d}"
                        tx_started = time.perf_counter()
                        connection.execute("BEGIN IMMEDIATE")
                        try:
                            post_journal(
                                connection, "2026-11-16", reference,
                                "اختبار إدخال متزامن لعشرين جهازًا",
                                [
                                    (1, "125.50", "0", "مدين الصندوق"),
                                    (2, "0", "125.50", "دائن الإيراد"),
                                ],
                                created_by=client_number,
                            )
                            connection.commit()
                        except Exception:
                            connection.rollback()
                            raise
                        finally:
                            with latency_lock:
                                writer_latencies.append(time.perf_counter() - tx_started)
                except Exception as exc:
                    with latency_lock:
                        failures.append(("writer", client_number, repr(exc)))
                    raise
                finally:
                    connection.close()

            def report_reader(reader_number):
                start.wait(timeout=20)
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
                            if row is None:
                                raise AssertionError("Financial report returned no period row")
                            if row["entry_count"] < 5000 or row["line_count"] < 10000:
                                raise AssertionError("Report did not see the complete historical ledger")
                            if row["debit_total"] != row["credit_total"]:
                                raise AssertionError("Report debit and credit totals do not balance")
                        except Exception as exc:
                            with latency_lock:
                                failures.append(("reader", reader_number, repr(exc)))
                            raise
                        finally:
                            with latency_lock:
                                reader_latencies.append(time.perf_counter() - query_started)
                finally:
                    connection.close()

            with ThreadPoolExecutor(max_workers=clients) as pool:
                futures = [
                    pool.submit(writer, number)
                    for number in range(1, writer_count + 1)
                ]
                futures.extend(
                    pool.submit(report_reader, number)
                    for number in range(1, reader_count + 1)
                )
                for future in futures:
                    future.result(timeout=120)

            elapsed = time.perf_counter() - began
            self.assertEqual(failures, [])
            self.assertEqual(len(writer_latencies), writer_count * posts_per_writer)
            self.assertEqual(len(reader_latencies), reader_count * reads_per_reader)

            writer_sorted = sorted(writer_latencies)
            reader_sorted = sorted(reader_latencies)
            writer_p95 = writer_sorted[int(len(writer_sorted) * 0.95) - 1]
            reader_p95 = reader_sorted[int(len(reader_sorted) * 0.95) - 1]
            print(
                "LAN_TWENTY_CLIENT_METRICS "
                f"clients={clients} writers={writer_count} readers={reader_count} "
                f"historical_entries=5000 new_entries={writer_count * posts_per_writer} "
                f"report_reads={len(reader_latencies)} elapsed_seconds={elapsed:.3f} "
                f"write_p95_seconds={writer_p95:.4f} write_max_seconds={max(writer_sorted):.4f} "
                f"report_p95_seconds={reader_p95:.4f} report_max_seconds={max(reader_sorted):.4f}"
            )
            self.assertLess(elapsed, 90, "20-client mixed workload exceeded 90 seconds")
            self.assertLess(writer_p95, 5, "95th percentile journal write latency exceeded five seconds")
            self.assertLess(max(writer_sorted), 20, "A journal write exceeded SQLite's configured lock timeout")
            self.assertLess(reader_p95, 3, "95th percentile financial report read exceeded three seconds")
            self.assertLess(max(reader_sorted), 10, "A financial report read exceeded ten seconds")

            verify = connect_database(db_path)
            try:
                self.assertEqual(verify.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(
                    verify.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0],
                    5000 + writer_count * posts_per_writer,
                )
                self.assertEqual(
                    verify.execute("SELECT COUNT(*) FROM journal_lines").fetchone()[0],
                    (5000 + writer_count * posts_per_writer) * 2,
                )
                self.assertEqual(
                    verify.execute("""
                        SELECT COUNT(*) FROM (
                            SELECT entry_id,
                                   DECIMAL_SUM(debit) AS debit_total,
                                   DECIMAL_SUM(credit) AS credit_total
                            FROM journal_lines GROUP BY entry_id
                            HAVING debit_total <> credit_total
                        )
                    """).fetchone()[0],
                    0,
                )
            finally:
                verify.close()


    def test_thirty_clients_mixed_subsystem_posting_and_financial_reports(self):
        """Stress 30 simultaneous clients with four posting streams and report reads.

        This is a DB/ledger concurrency test, not a claim to emulate HTTP/browser
        sessions or every business rule in the sales, purchasing, stock and payroll
        modules. Those module suites run separately in the same CI workflow.
        """
        import time

        # Keep all 30 clients active while avoiding 600 full-ledger scans against
        # 15,000 rows; this remains a heavier mixed workload than the 20-client test.
        writer_count = 15
        reader_count = 15
        client_count = writer_count + reader_count
        posts_per_writer = 20
        reads_per_reader = 30
        workload_types = ("sales", "purchases", "inventory", "payroll")

        with tempfile.TemporaryDirectory(prefix="nexus-thirty-client-load-") as folder:
            db_path = Path(folder) / "thirty-client.sqlite3"
            prepare_database(db_path)
            setup = connect_database(db_path)
            try:
                setup.executescript("""
                    CREATE INDEX idx_journal_entries_date ON journal_entries(entry_date);
                    CREATE INDEX idx_journal_entries_reference ON journal_entries(reference);
                    CREATE INDEX idx_journal_lines_entry_account ON journal_lines(entry_id, account_id);
                """)
                setup.execute("BEGIN")
                entries = []
                lines = []
                historical_count = 5000
                for number in range(1, historical_count + 1):
                    entries.append((
                        number, "2026-01-15", f"HIST30-{number:06d}",
                        "قيد تاريخي لاختبار تقارير الثلاثين جهازًا", "posted", 1,
                        "2026-01-15 10:00:00",
                    ))
                    lines.extend([
                        (number * 2 - 1, number, 1, "100.00", "0", "مدين"),
                        (number * 2, number, 2, "0", "100.00", "دائن"),
                    ])
                setup.executemany(
                    "INSERT INTO journal_entries(id,entry_date,reference,description,status,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    entries,
                )
                setup.executemany(
                    "INSERT INTO journal_lines(id,entry_id,account_id,debit,credit,memo) VALUES(?,?,?,?,?,?)",
                    lines,
                )
                setup.commit()
            finally:
                setup.close()

            start = threading.Barrier(client_count)
            metrics_lock = threading.Lock()
            writer_latencies = []
            writer_lock_wait_latencies = []
            writer_transaction_latencies = []
            report_latencies = []
            failures = []
            began = time.perf_counter()

            def writer(client_number):
                stream = workload_types[(client_number - 1) % len(workload_types)]
                start.wait(timeout=30)
                connection = connect_database(db_path, timeout=60, isolation_level="")
                try:
                    for sequence in range(posts_per_writer):
                        reference = f"THIRTY-{stream.upper()}-{client_number:02d}-{sequence:04d}"
                        tx_started = time.perf_counter()
                        lock_acquired = None
                        try:
                            connection.execute("BEGIN IMMEDIATE")
                            lock_acquired = time.perf_counter()
                            post_journal(
                                connection, "2026-11-16", reference,
                                f"اختبار ترحيل متزامن - {stream}",
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
                                writer_latencies.append(finished - tx_started)
                                if lock_acquired is not None:
                                    writer_lock_wait_latencies.append(lock_acquired - tx_started)
                                    writer_transaction_latencies.append(finished - lock_acquired)
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
                            if row is None:
                                raise AssertionError("Financial report returned no result")
                            if row["entry_count"] < historical_count:
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
                futures.extend(
                    pool.submit(report_reader, number)
                    for number in range(1, reader_count + 1)
                )
                for future in futures:
                    future.result(timeout=240)

            elapsed = time.perf_counter() - began
            self.assertEqual(failures, [])
            self.assertEqual(len(writer_latencies), writer_count * posts_per_writer)
            self.assertEqual(len(report_latencies), reader_count * reads_per_reader)

            ordered_writes = sorted(writer_latencies)
            ordered_lock_waits = sorted(writer_lock_wait_latencies)
            ordered_transaction_work = sorted(writer_transaction_latencies)
            ordered_reports = sorted(report_latencies)
            write_p95 = ordered_writes[int(len(ordered_writes) * 0.95) - 1]
            lock_wait_p95 = ordered_lock_waits[int(len(ordered_lock_waits) * 0.95) - 1]
            transaction_work_p95 = ordered_transaction_work[int(len(ordered_transaction_work) * 0.95) - 1]
            report_p95 = ordered_reports[int(len(ordered_reports) * 0.95) - 1]
            print(
                "LAN_THIRTY_CLIENT_METRICS "
                f"clients={client_count} writers={writer_count} readers={reader_count} "
                f"streams=sales,purchases,inventory,payroll historical_entries={historical_count} "
                f"new_entries={writer_count * posts_per_writer} report_reads={len(report_latencies)} "
                f"elapsed_seconds={elapsed:.3f} write_p95_seconds={write_p95:.4f} "
                f"write_max_seconds={max(ordered_writes):.4f} "
                f"lock_wait_p95_seconds={lock_wait_p95:.4f} "
                f"lock_wait_max_seconds={max(ordered_lock_waits):.4f} "
                f"transaction_work_p95_seconds={transaction_work_p95:.4f} "
                f"transaction_work_max_seconds={max(ordered_transaction_work):.4f} "
                f"report_p95_seconds={report_p95:.4f} report_max_seconds={max(ordered_reports):.4f}"
            )
            self.assertLess(elapsed, 150, "30-client mixed workload exceeded 150 seconds")
            self.assertLess(write_p95, 5, "95th percentile journal write exceeded five seconds")
            self.assertLess(max(ordered_writes), 60, "A journal write exceeded the configured SQLite lock wait")
            self.assertLess(report_p95, 5, "95th percentile financial report exceeded five seconds")
            self.assertLess(max(ordered_reports), 15, "A financial report read exceeded fifteen seconds")

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
                            SELECT entry_id,
                                   DECIMAL_SUM(debit) AS debit_total,
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
