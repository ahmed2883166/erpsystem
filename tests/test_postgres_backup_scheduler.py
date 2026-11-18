import os
import shutil
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from backup_scheduler import next_backup_datetime
from postgres_backup_scheduler import run_due_postgres_backup


class PostgreSQLBackupSchedulerTests(unittest.TestCase):
    def test_schedule_calculation_reuses_existing_daily_weekly_monthly_rules(self):
        now = datetime(2026, 11, 12, 10, 0)
        self.assertEqual(
            next_backup_datetime("daily", "23:15", now),
            datetime(2026, 11, 12, 23, 15),
        )
        self.assertEqual(
            next_backup_datetime("weekly", "02:30", now, weekday=6),
            datetime(2026, 11, 15, 2, 30),
        )

    @unittest.skipUnless(
        os.environ.get("NEXUS_TEST_POSTGRES_DSN")
        and shutil.which("pg_dump")
        and shutil.which("pg_restore"),
        "PostgreSQL server and native client tools are required",
    )
    def test_due_postgres_backup_runs_once_and_persists_result(self):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo

        admin_dsn = os.environ["NEXUS_TEST_POSTGRES_DSN"]
        database_name = "nexus_bksched_" + uuid.uuid4().hex[:12]
        with tempfile.TemporaryDirectory(prefix="nexus-pg-scheduler-") as folder:
            try:
                with psycopg.connect(admin_dsn, autocommit=True) as admin:
                    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
                dsn = make_conninfo(admin_dsn, dbname=database_name)
                with psycopg.connect(dsn) as connection:
                    connection.execute("""
                        CREATE TABLE company_settings(
                            setting_key TEXT PRIMARY KEY,
                            setting_value TEXT NOT NULL
                        )
                    """)
                    connection.execute("CREATE TABLE users(id BIGINT PRIMARY KEY)")
                    connection.execute("CREATE TABLE accounts(id BIGINT PRIMARY KEY)")
                    connection.execute("CREATE TABLE journal_entries(id BIGINT PRIMARY KEY)")
                    connection.execute("CREATE TABLE journal_lines(id BIGINT PRIMARY KEY)")
                    values = {
                        "backup_enabled": "1",
                        "backup_frequency": "daily",
                        "backup_time": "23:59",
                        "backup_weekday": "0",
                        "backup_month_day": "1",
                        "backup_interval_days": "1",
                        "backup_keep_count": "5",
                        "backup_next_run": (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds"),
                        "backup_last_status": "scheduled",
                        "backup_last_error": "",
                    }
                    connection.executemany(
                        "INSERT INTO company_settings(setting_key,setting_value) VALUES(%s,%s)",
                        list(values.items()),
                    )

                archive = run_due_postgres_backup(dsn, folder)
                self.assertIsNotNone(archive)
                self.assertTrue(Path(archive).is_file())
                self.assertIsNone(run_due_postgres_backup(dsn, folder))

                with psycopg.connect(dsn) as connection:
                    settings = dict(connection.execute(
                        "SELECT setting_key,setting_value FROM company_settings"
                    ).fetchall())
                self.assertEqual(settings["backup_last_status"], "success")
                self.assertEqual(settings["backup_last_file"], Path(archive).name)
                self.assertEqual(settings["backup_last_error"], "")
            finally:
                with psycopg.connect(admin_dsn, autocommit=True) as admin:
                    admin.execute(
                        sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name))
                    )


if __name__ == "__main__":
    unittest.main()
