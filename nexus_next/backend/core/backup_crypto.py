"""Authenticated, streaming AES-256-GCM encryption for offline backup files."""
from __future__ import annotations
import os
import tempfile
from pathlib import Path
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
MAGIC = b"NEXUSENC1"
SALT_SIZE, NONCE_SIZE, TAG_SIZE = 16, 12, 16
CHUNK_SIZE = 1024 * 1024
class BackupCryptoError(ValueError):
    """Invalid encrypted backup or failed authentication."""
def _derive_key(passphrase: str, salt: bytes) -> bytes:
    if len(passphrase) < 12:
        raise BackupCryptoError("عبارة التشفير يجب ألا تقل عن 12 حرفًا.")
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(passphrase.encode("utf-8"))
def encrypt_backup(source: str | Path, destination: str | Path, passphrase: str) -> Path:
    src, dst = Path(source), Path(destination)
    if not src.is_file(): raise BackupCryptoError("ملف النسخة الاحتياطية غير موجود.")
    if src.resolve() == dst.resolve(): raise BackupCryptoError("ملف الإخراج يجب أن يختلف عن المصدر.")
    if dst.exists(): raise BackupCryptoError("ملف التشفير موجود بالفعل؛ لن يتم استبداله.")
    dst.parent.mkdir(parents=True, exist_ok=True)
    salt, nonce = os.urandom(SALT_SIZE), os.urandom(NONCE_SIZE)
    encryptor = Cipher(algorithms.AES(_derive_key(passphrase, salt)), modes.GCM(nonce)).encryptor()
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=dst.parent, prefix=".nexus-encrypt-", delete=False) as out:
            temp = Path(out.name); out.write(MAGIC + salt + nonce)
            with src.open("rb") as inp:
                while chunk := inp.read(CHUNK_SIZE): out.write(encryptor.update(chunk))
            out.write(encryptor.finalize()); out.write(encryptor.tag); out.flush(); os.fsync(out.fileno())
        os.replace(temp, dst)
        return dst
    except Exception:
        if temp: temp.unlink(missing_ok=True)
        raise
def decrypt_backup(source: str | Path, destination: str | Path, passphrase: str) -> Path:
    src, dst = Path(source), Path(destination)
    if not src.is_file(): raise BackupCryptoError("ملف النسخة المشفرة غير موجود.")
    if src.resolve() == dst.resolve(): raise BackupCryptoError("ملف الإخراج يجب أن يختلف عن المصدر.")
    if dst.exists(): raise BackupCryptoError("ملف الاستعادة موجود بالفعل؛ لن يتم استبداله.")
    dst.parent.mkdir(parents=True, exist_ok=True); temp = None
    try:
        with src.open("rb") as inp:
            header = inp.read(len(MAGIC) + SALT_SIZE + NONCE_SIZE)
            if len(header) != len(MAGIC)+SALT_SIZE+NONCE_SIZE or not header.startswith(MAGIC):
                raise BackupCryptoError("صيغة ملف النسخة المشفرة غير معروفة.")
            salt = header[len(MAGIC):len(MAGIC)+SALT_SIZE]; nonce = header[-NONCE_SIZE:]
            inp.seek(0, os.SEEK_END); total = inp.tell(); size = total-len(header)-TAG_SIZE
            if size < 0: raise BackupCryptoError("ملف النسخة المشفرة تالف.")
            inp.seek(total-TAG_SIZE); tag = inp.read(TAG_SIZE); inp.seek(len(header))
            decryptor = Cipher(algorithms.AES(_derive_key(passphrase, salt)), modes.GCM(nonce, tag)).decryptor()
            with tempfile.NamedTemporaryFile(dir=dst.parent, prefix=".nexus-decrypt-", delete=False) as out:
                temp = Path(out.name); remaining = size
                while remaining:
                    chunk = inp.read(min(CHUNK_SIZE, remaining))
                    if not chunk: raise BackupCryptoError("ملف النسخة المشفرة غير مكتمل.")
                    remaining -= len(chunk); out.write(decryptor.update(chunk))
                out.write(decryptor.finalize()); out.flush(); os.fsync(out.fileno())
        os.replace(temp, dst); return dst
    except BackupCryptoError:
        if temp: temp.unlink(missing_ok=True)
        raise
    except Exception as exc:
        if temp: temp.unlink(missing_ok=True)
        raise BackupCryptoError("فشل فك التشفير: كلمة المرور غير صحيحة أو الملف تم التلاعب به.") from exc
