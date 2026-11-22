"""Offline, owner-signed installation licenses for Windows deployments.

This module deliberately does not enforce licenses at application startup yet.
Enforcement is added only after the owner key ceremony and recovery workflow exist.
"""
from __future__ import annotations

import base64
import hashlib
import json
import platform
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


LICENSE_SCHEMA_VERSION = 1


class LicenseError(ValueError):
    """Raised when an offline license is malformed, invalid, or for another server."""


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def generate_owner_keypair(private_key_path: str | Path, public_key_path: str | Path,
                           passphrase: str) -> None:
    """Create an encrypted Ed25519 private key and its public verification key."""
    if len(passphrase) < 12:
        raise LicenseError("يجب أن تتكون عبارة حماية مفتاح المالك من 12 حرفًا على الأقل.")
    private_path, public_path = Path(private_key_path), Path(public_key_path)
    if private_path.exists() or public_path.exists():
        raise LicenseError("ملف المفتاح موجود بالفعل؛ لن يتم استبداله.")
    private_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(passphrase.encode("utf-8")),
    )
    public_bytes = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    private_path.write_bytes(private_bytes)
    try:
        public_path.write_bytes(public_bytes)
    except Exception:
        private_path.unlink(missing_ok=True)
        raise


def fingerprint_from_machine_guid(machine_guid: str) -> str:
    """Derive a non-reversible Windows installation fingerprint from MachineGuid."""
    normalized = machine_guid.strip().lower()
    if not normalized:
        raise LicenseError("تعذر قراءة معرّف جهاز Windows.")
    return hashlib.sha256(("admrail-nexus-v1:" + normalized).encode("utf-8")).hexdigest()


def windows_machine_fingerprint() -> str:
    """Read the Windows MachineGuid; no network or external service is used."""
    if platform.system() != "Windows":
        raise LicenseError("بصمة التثبيت الحالية مدعومة على Windows فقط.")
    import winreg  # imported only on Windows

    key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\\Microsoft\\Cryptography")
    try:
        machine_guid, _ = winreg.QueryValueEx(key, "MachineGuid")
    finally:
        winreg.CloseKey(key)
    return fingerprint_from_machine_guid(str(machine_guid))


def create_signed_license(private_key_path: str | Path, passphrase: str,
                          installation_id: str, machine_fingerprint: str,
                          license_id: str, issued_at: str,
                          features: list[str] | None = None) -> str:
    """Sign a perpetual offline license for one installation and one machine."""
    if not all((installation_id.strip(), machine_fingerprint.strip(), license_id.strip(), issued_at.strip())):
        raise LicenseError("بيانات الترخيص الأساسية غير مكتملة.")
    private_key = serialization.load_pem_private_key(
        Path(private_key_path).read_bytes(), password=passphrase.encode("utf-8")
    )
    if not isinstance(private_key, Ed25519PrivateKey):
        raise LicenseError("مفتاح المالك ليس من النوع المدعوم.")
    payload = {
        "schema_version": LICENSE_SCHEMA_VERSION,
        "license_id": license_id,
        "installation_id": installation_id,
        "machine_fingerprint": machine_fingerprint,
        "issued_at": issued_at,
        "features": sorted(set(features or [])),
        "offline": True,
        "perpetual": True,
    }
    encoded = _canonical_json(payload)
    signature = private_key.sign(encoded)
    return _b64encode(encoded) + "." + _b64encode(signature)


def verify_signed_license(token: str, public_key_path: str | Path,
                          installation_id: str, machine_fingerprint: str) -> dict[str, Any]:
    """Verify signature and installation binding; never contacts a remote service."""
    try:
        encoded_payload, encoded_signature = token.strip().split(".", 1)
        payload_bytes, signature = _b64decode(encoded_payload), _b64decode(encoded_signature)
        public_key = serialization.load_pem_public_key(Path(public_key_path).read_bytes())
        if not isinstance(public_key, Ed25519PublicKey):
            raise LicenseError("مفتاح التحقق العام غير صالح.")
        public_key.verify(signature, payload_bytes)
        payload = json.loads(payload_bytes.decode("utf-8"))
    except InvalidSignature as exc:
        raise LicenseError("توقيع الترخيص غير صحيح.") from exc
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        if isinstance(exc, LicenseError):
            raise
        raise LicenseError("ملف الترخيص تالف أو غير قابل للقراءة.") from exc

    if payload.get("schema_version") != LICENSE_SCHEMA_VERSION:
        raise LicenseError("إصدار ملف الترخيص غير مدعوم.")
    if payload.get("offline") is not True or payload.get("perpetual") is not True:
        raise LicenseError("خصائص الترخيص غير مطابقة للسياسة المحلية.")
    if payload.get("installation_id") != installation_id:
        raise LicenseError("هذا الترخيص صادر لتثبيت آخر.")
    if payload.get("machine_fingerprint") != machine_fingerprint:
        raise LicenseError("هذا الترخيص صادر لجهاز آخر؛ يلزم اعتماد المالك للنقل.")
    return payload
