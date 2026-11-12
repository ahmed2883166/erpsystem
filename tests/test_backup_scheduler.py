import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from backup_scheduler import create_backup_archive, next_backup_datetime, run_due_backup


class BackupScheduleTests(unittest.TestCase):
    def test_daily_schedule_uses_selected_time(self):
        current = datetime(2026, 11, 12, 10, 0)
        self.assertEqual(
            next_backup_datetime("daily", "23:15", current),
            datetime(2026, 11, 12, 23, 15),
        )

    def test_weekly_schedule_uses_selected_weekday(self):
        current = datetime(2026, 11, 12, 10, 0)  # Thursday
        self.assertEqual(
            next_backup_datetime("weekly", "02:30", current, weekday=6),
            datetime(2026, 11, 15, 2, 30),
        )

    def test_monthly_schedule_clamps_day_to_month_end(self):
        current = datetime(2026, 2, 1, 8, 0)
        self.assertEqual(
            next_backup_datetime("monthly", "23:00", current, month_day=31),
            datetime(2026, 2, 28, 23, 0),
        )

    def test_custom_interval_schedule(self):
        current = datetime(2026, 11, 12, 23, 30)
        self.assertEqual(
            next_backup_datetime("every_n_days", "22:00", current, interval_days=4),
            datetime(2026, 11, 16, 22, 0),
        )

    def test_backup_archive_contains_integrity_checked_database(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "nexus.db"
            conn = sqlite3.connect(database)
            conn.execute("CREATE TABLE sample(value TEXT)")
            conn.execute("INSERT INTO sample VALUES('preserved')")
            conn.commit()
            conn.close()
            archive = create_backup_archive(database, root / "backups", 10)
            self.assertTrue(archive.exists())
            with __import__("zipfile").ZipFile(archive) as zipped:
                self.assertIn("nexus.db", zipped.namelist())
                self.assertIn("manifest.json", zipped.namelist())
                extracted = root / "restored.db"
                extracted.write_bytes(zipped.read("nexus.db"))
            restored = sqlite3.connect(extracted)
            self.assertEqual(restored.execute("SELECT value FROM sample").fetchone()[0], "preserved")
            restored.close()

    def test_due_backup_runs_once_and_persists_status(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "nexus.db"
            conn = sqlite3.connect(database)
            conn.execute("CREATE TABLE company_settings(setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL)")
            values = {
                "backup_enabled": "1", "backup_frequency": "daily",
                "backup_time": "23:59", "backup_weekday": "0",
                "backup_month_day": "1", "backup_interval_days": "1",
                "backup_keep_count": "5",
                "backup_next_run": (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds"),
                "backup_last_status": "scheduled", "backup_last_error": "",
            }
            conn.executemany("INSERT INTO company_settings VALUES(?,?)", list(values.items()))
            conn.execute("CREATE TABLE sample(value TEXT)")
            conn.commit()
            conn.close()

            result = run_due_backup(database, root / "backups")
            self.assertIsNotNone(result)
            self.assertTrue(result.exists())
            conn = sqlite3.connect(database)
            settings = dict(conn.execute("SELECT setting_key,setting_value FROM company_settings"))
            conn.close()
            self.assertEqual(settings["backup_last_status"], "success")
            self.assertEqual(settings["backup_last_file"], result.name)
            self.assertIsNone(run_due_backup(database, root / "backups"))


if __name__ == "__main__":
    unittest.main()
