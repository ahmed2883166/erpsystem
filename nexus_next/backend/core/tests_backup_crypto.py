from pathlib import Path
import pytest
from core.backup_crypto import BackupCryptoError, _publish_no_replace, decrypt_backup, encrypt_backup
def test_backup_encrypt_decrypt_round_trip(tmp_path: Path):
    original=tmp_path/"database.dump"; encrypted=tmp_path/"database.enc"; restored=tmp_path/"restored.dump"
    payload=(b"NEXUS POSTGRES BACKUP\x00"+bytes(range(256)))*9000; original.write_bytes(payload)
    encrypt_backup(original,encrypted,"a long and secure backup phrase")
    assert encrypted.read_bytes()!=original.read_bytes()
    decrypt_backup(encrypted,restored,"a long and secure backup phrase"); assert restored.read_bytes()==payload
def test_wrong_password_does_not_publish_plaintext(tmp_path: Path):
    src=tmp_path/"src"; enc=tmp_path/"enc"; out=tmp_path/"out"; src.write_bytes(b"financial database"*1000)
    encrypt_backup(src,enc,"correct secure phrase")
    with pytest.raises(BackupCryptoError): decrypt_backup(enc,out,"incorrect secure phrase")
    assert not out.exists()
def test_tampered_backup_is_rejected(tmp_path: Path):
    src=tmp_path/"src"; enc=tmp_path/"enc"; out=tmp_path/"out"; src.write_bytes(b"financial database"*1000)
    encrypt_backup(src,enc,"correct secure phrase"); data=bytearray(enc.read_bytes()); data[len(data)//2]^=1; enc.write_bytes(data)
    with pytest.raises(BackupCryptoError): decrypt_backup(enc,out,"correct secure phrase")
    assert not out.exists()
def test_encryption_never_overwrites_existing_files(tmp_path: Path):
    src=tmp_path/"src"; enc=tmp_path/"enc"; src.write_bytes(b"backup"); enc.write_bytes(b"keep this")
    with pytest.raises(BackupCryptoError): encrypt_backup(src,enc,"correct secure phrase")
    assert enc.read_bytes()==b"keep this"


def test_atomic_publication_refuses_a_destination_created_after_initial_check(tmp_path: Path):
    temporary = tmp_path / ".temporary-encrypted"
    destination = tmp_path / "existing-backup.nexusenc"
    temporary.write_bytes(b"new encrypted content")
    destination.write_bytes(b"preserve existing backup")
    with pytest.raises(FileExistsError):
        _publish_no_replace(temporary, destination)
    assert destination.read_bytes() == b"preserve existing backup"
    assert temporary.read_bytes() == b"new encrypted content"


def test_decryption_never_overwrites_an_existing_restore_file(tmp_path: Path):
    source = tmp_path / "source.dump"
    encrypted = tmp_path / "source.dump.nexusenc"
    restored = tmp_path / "restored.dump"
    source.write_bytes(b"accounting archive" * 1000)
    encrypt_backup(source, encrypted, "a long and secure backup phrase")
    restored.write_bytes(b"keep existing restore file")
    with pytest.raises(BackupCryptoError):
        decrypt_backup(encrypted, restored, "a long and secure backup phrase")
    assert restored.read_bytes() == b"keep existing restore file"
