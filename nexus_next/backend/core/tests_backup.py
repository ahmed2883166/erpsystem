from django.contrib.auth import get_user_model
from django.test import TestCase
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch\nfrom types import SimpleNamespace
from rest_framework.test import APIClient

from core.models import BackupConfiguration, BackupRun


class BackupConfigurationApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = "/api/backup/configuration/"
        self.user = get_user_model().objects.create_user(username="operator", password="safe-password-123")
        self.admin = get_user_model().objects.create_superuser(
            username="system-admin", email="admin@example.test", password="safe-password-123"
        )

    def test_scheduled_backup_command_records_only_encrypted_archives(self):
        from django.core.management import call_command, CommandError
        with TemporaryDirectory() as folder:
            encrypted = Path(folder) / "nexus-daily.dump.nexusenc"
            encrypted.write_bytes(b"encrypted archive" * 200)
            call_command("record_backup_run", file=str(encrypted), verbosity=0)
            run = BackupRun.objects.get(backup_file=str(encrypted.resolve()))
            self.assertEqual(run.status, BackupRun.Status.SUCCESS)
            self.assertIsNone(run.started_by)
            plaintext = Path(folder) / "nexus-old.dump"
            plaintext.write_bytes(b"plaintext archive" * 200)
            with self.assertRaises(CommandError):
                call_command("record_backup_run", file=str(plaintext), verbosity=0)

    def test_platform_owner_cannot_view_tenant_backup_configuration_or_history(self):
        from companies.models import PlatformOwner
        owner = get_user_model().objects.create_superuser(
            username="platform-owner-backup", email="owner-backup@example.test",
            password="safe-password-123",
        )
        PlatformOwner.objects.create(user=owner)
        self.client.force_authenticate(owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_manual_backup_reuses_scheduled_script_history_record(self):
        self.client.force_authenticate(self.admin)
        BackupConfiguration.objects.create(
            backup_path=r"D:\\NEXUS_Backups", enabled=True,
        )
        with TemporaryDirectory() as folder:
            archive = Path(folder) / "manual.dump.nexusenc"

            def run_script(*args, **kwargs):
                archive.write_bytes(b"encrypted verified archive" * 100)
                BackupRun.objects.create(
                    status=BackupRun.Status.SUCCESS,
                    backup_file=str(archive.resolve()),
                    size_bytes=archive.stat().st_size,
                    details="scheduled script record",
                )
                return SimpleNamespace(
                    returncode=0,
                    stdout=f"Encrypted backup verified: {archive.resolve()}\\n",
                    stderr="",
                )

            with patch("core.backup_api.sys.platform", "win32"), patch(
                "core.backup_api.subprocess.run", side_effect=run_script,
            ):
                response = self.client.post(self.url, {}, format="json")
            self.assertEqual(response.status_code, 201, response.data)
            self.assertEqual(BackupRun.objects.count(), 1)
            run = BackupRun.objects.get()
            self.assertEqual(run.started_by, self.admin)
            self.assertIn("manual.dump.nexusenc", run.backup_file)

    def test_regular_user_can_read_but_cannot_change_destination(self):
        self.client.force_authenticate(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["can_manage"])
        update = self.client.patch(self.url, {"backup_path": r"D:\NEXUS_Backups"}, format="json")
        self.assertEqual(update.status_code, 403)

    def test_only_superuser_can_set_external_destination(self):
        self.client.force_authenticate(self.admin)
        response = self.client.patch(
            self.url,
            {"backup_path": r"D:\NEXUS_Backups", "schedule": "weekly", "enabled": True},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["backup_path"], r"D:\NEXUS_Backups")
        self.assertEqual(response.data["schedule"], "weekly")
        self.assertTrue(BackupConfiguration.objects.filter(backup_path=r"D:\NEXUS_Backups").exists())

    def test_c_drive_is_rejected(self):
        self.client.force_authenticate(self.admin)
        response = self.client.patch(self.url, {"backup_path": r"C:\NEXUS_Backups"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_company_admin_role_does_not_grant_system_backup_admin(self):
        self.assertFalse(self.user.is_superuser)
        self.client.force_authenticate(self.user)
        response = self.client.patch(self.url, {"enabled": False}, format="json")
        self.assertEqual(response.status_code, 403)

    def test_regular_user_cannot_start_backup(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, 403)


    def test_reset_requires_recent_verified_backup_and_password(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            "/api/backup/reset/",
            {"password": "safe-password-123", "confirmation": "تصفير الحركات"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_reset_requires_exact_confirmation_and_superuser(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(
            "/api/backup/reset/",
            {"password": "safe-password-123", "confirmation": "تصفير الحركات"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_reset_preview_resolves_all_operational_models(self):
        self.client.force_authenticate(self.admin)
        response = self.client.get("/api/backup/reset-preview/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("accounting.JournalEntry", response.data["counts"])
        self.assertIn("inventory.StockMovement", response.data["counts"])
        self.assertIn("hr.PayrollCycle", response.data["counts"])

    def test_reset_service_runs_cleanly_when_no_operational_rows_exist(self):
        from core.reset_service import reset_operational_data
        deleted = reset_operational_data()
        self.assertIn("accounting.JournalEntry", deleted)
        self.assertTrue(all(value == 0 for value in deleted.values()))

    def test_reset_executes_only_after_a_real_recent_backup(self):
        self.client.force_authenticate(self.admin)
        with TemporaryDirectory() as folder:
            archive = Path(folder) / "verified.dump"
            archive.write_bytes(b"x" * 2048)
            BackupRun.objects.create(
                status=BackupRun.Status.SUCCESS,
                backup_file=str(archive),
                size_bytes=archive.stat().st_size,
                started_by=self.admin,
            )
            with patch("core.backup_api.reset_operational_data", return_value={"accounting.JournalEntry": 0}) as reset:
                response = self.client.post(
                    "/api/backup/reset/",
                    {"password": "safe-password-123", "confirmation": "تصفير الحركات"},
                    format="json",
                )
            self.assertEqual(response.status_code, 200)
            reset.assert_called_once()
