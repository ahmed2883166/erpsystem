from pathlib import Path
import pytest
from core.backup_crypto import BackupCryptoError, decrypt_backup, encrypt_backup
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
