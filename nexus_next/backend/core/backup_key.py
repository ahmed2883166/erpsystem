"""Windows DPAPI protection for the unattended PostgreSQL backup key."""
from __future__ import annotations

import base64
import ctypes
import os
from pathlib import Path

from .backup_crypto import BackupCryptoError, decrypt_recovery_key, encrypt_recovery_key

BACKUP_KEY_BYTES = 32
DPAPI_ENTROPY = b"ADMRAIL-NEXUS-BACKUP-KEY-v1"
CRYPTPROTECT_LOCAL_MACHINE = 0x4


def default_backup_key_path() -> Path:
    from django.conf import settings
    return Path(settings.BASE_DIR) / ".secrets" / "backup-key.dpapi"


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    if os.name != "nt":
        raise BackupCryptoError("حماية مفتاح النسخ التلقائي متاحة على Windows فقط.")
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    def blob(value: bytes):
        buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
        return DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer

    crypt32 = ctypes.WinDLL("crypt32.dll", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    source, source_buffer = blob(data)
    entropy, entropy_buffer = blob(DPAPI_ENTROPY)
    output = DataBlob()
    if protect:
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(DataBlob), wintypes.LPCWSTR, ctypes.POINTER(DataBlob),
            wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(DataBlob),
        ]
        crypt32.CryptProtectData.restype = wintypes.BOOL
        ok = crypt32.CryptProtectData(
            ctypes.byref(source), "NEXUS unattended backup key",
            ctypes.byref(entropy), None, None, CRYPTPROTECT_LOCAL_MACHINE, ctypes.byref(output),
        )
    else:
        description = wintypes.LPWSTR()
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(DataBlob), ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(DataBlob), wintypes.LPVOID, wintypes.LPVOID,
            wintypes.DWORD, ctypes.POINTER(DataBlob),
        ]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(source), ctypes.byref(description), ctypes.byref(entropy),
            None, None, 0, ctypes.byref(output),
        )
        if description:
            kernel32.LocalFree(ctypes.cast(description, ctypes.c_void_p))
    if not ok:
        raise OSError(ctypes.get_last_error(), "Windows DPAPI operation failed")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))


def protect_machine_key(key: bytes) -> bytes:
    if len(key) != BACKUP_KEY_BYTES:
        raise BackupCryptoError("مفتاح النسخ الاحتياطي يجب أن يكون 256 بت.")
    return _dpapi(key, protect=True)


def unprotect_machine_key(protected: bytes) -> bytes:
    key = _dpapi(protected, protect=False)
    if len(key) != BACKUP_KEY_BYTES:
        raise BackupCryptoError("ملف مفتاح النسخ الاحتياطي غير صالح.")
    return key


def initialize_backup_key(
    key_file: str | Path,
    recovery_file: str | Path,
    recovery_passphrase: str,
) -> tuple[Path, Path]:
    """Create a machine-bound key plus a separately encrypted USB recovery copy."""
    if os.name != "nt":
        raise BackupCryptoError("تهيئة مفتاح النسخ التلقائي متاحة على Windows فقط.")
    target, recovery = Path(key_file), Path(recovery_file)
    if target.exists() or recovery.exists():
        raise BackupCryptoError("ملف المفتاح أو الاسترداد موجود بالفعل؛ لن يتم استبداله.")
    if target.resolve() == recovery.resolve():
        raise BackupCryptoError("ملف المفتاح وملف الاسترداد يجب أن يكونا منفصلين.")
    key = os.urandom(BACKUP_KEY_BYTES)
    protected = protect_machine_key(key)
    recovery.parent.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    encrypt_recovery_key(key, recovery, recovery_passphrase)
    try:
        with target.open("xb") as stream:
            stream.write(protected)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        recovery.unlink(missing_ok=True)
        raise
    return target, recovery


def restore_backup_key(
    recovery_file: str | Path,
    key_file: str | Path,
    recovery_passphrase: str,
) -> Path:
    """Rebind the offline recovery key to the current Windows installation."""
    if os.name != "nt":
        raise BackupCryptoError("استعادة مفتاح النسخ التلقائي متاحة على Windows فقط.")
    target = Path(key_file)
    if target.exists():
        raise BackupCryptoError("يوجد مفتاح محلي بالفعل؛ لن يتم استبداله.")
    key = decrypt_recovery_key(recovery_file, recovery_passphrase)
    protected = protect_machine_key(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as stream:
        stream.write(protected)
        stream.flush()
        os.fsync(stream.fileno())
    return target


def machine_backup_passphrase(key_file: str | Path | None = None) -> str:
    """Return a high-entropy passphrase derived from the DPAPI-unwrapped key."""
    path = Path(key_file) if key_file else default_backup_key_path()
    if not path.is_file():
        raise BackupCryptoError(
            "مفتاح التشفير التلقائي غير مهيأ. شغّل init_backup_encryption أولًا."
        )
    key = unprotect_machine_key(path.read_bytes())
    return base64.urlsafe_b64encode(key).decode("ascii")
