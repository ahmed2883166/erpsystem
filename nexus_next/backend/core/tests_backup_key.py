import base64
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from core.backup_crypto import BackupCryptoError, decrypt_recovery_key, encrypt_recovery_key
from core import backup_key


class BackupKeyTests(TestCase):
    def test_recovery_key_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            recovery = Path(folder) / "owner-usb.nexuskey"
            key = os.urandom(32)
            encrypt_recovery_key(key, recovery, "long and safe recovery phrase")
            self.assertNotEqual(recovery.read_bytes(), key)
            self.assertEqual(
                decrypt_recovery_key(recovery, "long and safe recovery phrase"), key
            )

    def test_recovery_key_wrong_password_and_tampering_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            recovery = Path(folder) / "owner-usb.nexuskey"
            encrypt_recovery_key(os.urandom(32), recovery, "long and safe recovery phrase")
            with self.assertRaises(BackupCryptoError):
                decrypt_recovery_key(recovery, "incorrect recovery phrase")
            payload = bytearray(recovery.read_bytes())
            payload[-20] ^= 0x01
            recovery.write_bytes(payload)
            with self.assertRaises(BackupCryptoError):
                decrypt_recovery_key(recovery, "long and safe recovery phrase")

    def test_recovery_key_refuses_to_overwrite_existing_file(self):
        with tempfile.TemporaryDirectory() as folder:
            recovery = Path(folder) / "owner-usb.nexuskey"
            recovery.write_bytes(b"keep existing")
            with self.assertRaises(BackupCryptoError):
                encrypt_recovery_key(os.urandom(32), recovery, "long and safe recovery phrase")
            self.assertEqual(recovery.read_bytes(), b"keep existing")

    def test_machine_backup_passphrase_uses_unwrapped_key(self):
        with tempfile.TemporaryDirectory() as folder:
            key_file = Path(folder) / "backup-key.dpapi"
            key_file.write_bytes(b"protected-key")
            key = bytes(range(32))
            with patch("core.backup_key.unprotect_machine_key", return_value=key):
                actual = backup_key.machine_backup_passphrase(key_file)
            self.assertEqual(actual, base64.urlsafe_b64encode(key).decode("ascii"))

    def test_machine_key_protection_refuses_non_windows(self):
        if os.name == "nt":
            self.skipTest("non-Windows guard test")
        with self.assertRaises(BackupCryptoError):
            backup_key.protect_machine_key(os.urandom(32))

    def test_dpapi_round_trip_on_windows(self):
        if os.name != "nt":
            self.skipTest("Windows DPAPI integration test")
        key = os.urandom(32)
        protected = backup_key.protect_machine_key(key)
        self.assertNotEqual(protected, key)
        self.assertEqual(backup_key.unprotect_machine_key(protected), key)

    def test_management_command_encrypts_verifies_and_removes_plaintext_source(self):
        from django.core.management import call_command
        from core.backup_crypto import decrypt_backup

        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "database.dump"
            encrypted = Path(folder) / "database.dump.nexusenc"
            restored = Path(folder) / "restored.dump"
            payload = (b"NEXUS postgres archive" * 5000)
            source.write_bytes(payload)
            phrase = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
            with patch(
                "core.management.commands.encrypt_backup_file.machine_backup_passphrase",
                return_value=phrase,
            ):
                call_command(
                    "encrypt_backup_file", input=str(source), output=str(encrypted),
                    delete_source=True, verbosity=0,
                )
            self.assertFalse(source.exists())
            self.assertTrue(encrypted.is_file())
            decrypt_backup(encrypted, restored, phrase)
            self.assertEqual(restored.read_bytes(), payload)

    def test_legacy_archive_migration_updates_existing_backup_history(self):
        from django.core.management import call_command
        from core.models import BackupRun

        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "legacy.dump"
            encrypted = Path(folder) / "legacy.dump.nexusenc"
            source.write_bytes(b"legacy PostgreSQL dump" * 1000)
            run = BackupRun.objects.create(
                status=BackupRun.Status.SUCCESS,
                backup_file=str(source),
                size_bytes=source.stat().st_size,
            )
            phrase = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
            with patch(
                "core.management.commands.encrypt_backup_file.machine_backup_passphrase",
                return_value=phrase,
            ):
                call_command(
                    "encrypt_backup_file", input=str(source), output=str(encrypted),
                    delete_source=True, update_history=True, verbosity=0,
                )
            run.refresh_from_db()
            self.assertFalse(source.exists())
            self.assertEqual(run.backup_file, str(encrypted.resolve()))
            self.assertEqual(run.size_bytes, encrypted.stat().st_size)
