from pathlib import Path

import pytest

from core.offline_license import (
    LicenseError,
    create_signed_license,
    fingerprint_from_machine_guid,
    generate_owner_keypair,
    verify_signed_license,
)


def test_owner_license_is_signed_and_bound_to_installation_and_machine(tmp_path: Path):
    private_key = tmp_path / "owner-usb" / "owner-private.pem"
    public_key = tmp_path / "public" / "owner-public.pem"
    generate_owner_keypair(private_key, public_key, "correct horse battery")
    fingerprint = fingerprint_from_machine_guid("A1B2-C3D4")
    token = create_signed_license(
        private_key, "correct horse battery", "install-001", fingerprint,
        "license-001", "2026-11-22T12:00:00Z", ["accounting", "inventory"],
    )

    claims = verify_signed_license(token, public_key, "install-001", fingerprint)
    assert claims["license_id"] == "license-001"
    assert claims["offline"] is True
    assert claims["features"] == ["accounting", "inventory"]

    with pytest.raises(LicenseError, match="تثبيت آخر"):
        verify_signed_license(token, public_key, "install-002", fingerprint)
    with pytest.raises(LicenseError, match="جهاز آخر"):
        verify_signed_license(token, public_key, "install-001", "different-machine")


def test_modified_license_payload_is_rejected(tmp_path: Path):
    private_key = tmp_path / "owner.pem"
    public_key = tmp_path / "owner.pub"
    generate_owner_keypair(private_key, public_key, "another secure passphrase")
    token = create_signed_license(
        private_key, "another secure passphrase", "install-001", "machine-001",
        "license-001", "2026-11-22T12:00:00Z",
    )
    payload, signature = token.split(".", 1)
    tampered = payload[:-2] + ("AA" if not payload.endswith("AA") else "BB") + "." + signature
    with pytest.raises(LicenseError):
        verify_signed_license(tampered, public_key, "install-001", "machine-001")


def test_owner_key_creation_never_overwrites_existing_key(tmp_path: Path):
    private_key = tmp_path / "owner.pem"
    public_key = tmp_path / "owner.pub"
    generate_owner_keypair(private_key, public_key, "strong passphrase 123")
    with pytest.raises(LicenseError, match="موجود بالفعل"):
        generate_owner_keypair(private_key, public_key, "strong passphrase 456")


def test_owner_passphrase_must_be_strong(tmp_path: Path):
    with pytest.raises(LicenseError, match="12 حرفًا"):
        generate_owner_keypair(tmp_path / "owner.pem", tmp_path / "owner.pub", "short")
