import base64
import os
from pathlib import Path

import pytest

from core.backup_crypto import (
    BackupCryptoError,
    decrypt_recovery_key,
    encrypt_recovery_key,
)
from core import backup_key


def test_recovery_key_round_trip(tmp_path: Path):
    key = os.urandom(32)
    recovery = tmp_path / "owner-usb.nexuskey"
    encrypt_recovery_key(key, recovery, "long and safe recovery phrase")
    assert recovery.read_bytes() != key
    assert decrypt_recovery_key(recovery, "long and safe recovery phrase") == key


def test_recovery_key_wrong_password_and_tampering_are_rejected(tmp_path: Path):
    recovery = tmp_path / "owner-usb.nexuskey"
    encrypt_recovery_key(os.urandom(32), recovery, "long and safe recovery phrase")
    with pytest.raises(BackupCryptoError):
        decrypt_recovery_key(recovery, "incorrect recovery phrase")
    payload = bytearray(recovery.read_bytes())
    payload[-20] ^= 0x01
    recovery.write_bytes(payload)
    with pytest.raises(BackupCryptoError):
        decrypt_recovery_key(recovery, "long and safe recovery phrase")


def test_recovery_key_refuses_to_overwrite_existing_file(tmp_path: Path):
    recovery = tmp_path / "owner-usb.nexuskey"
    recovery.write_bytes(b"keep existing")
    with pytest.raises(BackupCryptoError):
        encrypt_recovery_key(os.urandom(32), recovery, "long and safe recovery phrase")
    assert recovery.read_bytes() == b"keep existing"


def test_machine_backup_passphrase_uses_unwrapped_key(tmp_path: Path, monkeypatch):
    key_file = tmp_path / "backup-key.dpapi"
    key_file.write_bytes(b"protected-key")
    key = bytes(range(32))
    monkeypatch.setattr(backup_key, "unprotect_machine_key", lambda value: key)
    actual = backup_key.machine_backup_passphrase(key_file)
    assert actual == base64.urlsafe_b64encode(key).decode("ascii")


@pytest.mark.skipif(os.name == "nt", reason="non-Windows guard test")
def test_machine_key_protection_refuses_non_windows():
    with pytest.raises(BackupCryptoError):
        backup_key.protect_machine_key(os.urandom(32))
