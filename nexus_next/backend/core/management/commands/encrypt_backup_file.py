import hashlib
import tempfile
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.backup_crypto import decrypt_backup, encrypt_backup
from core.backup_key import machine_backup_passphrase


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.digest()


class Command(BaseCommand):
    help = "Encrypt a PostgreSQL dump using the local Windows DPAPI backup key."

    def add_arguments(self, parser):
        parser.add_argument("--input", required=True)
        parser.add_argument("--output", required=True)
        parser.add_argument("--delete-source", action="store_true")

    def handle(self, *args, **options):
        source, destination = Path(options["input"]), Path(options["output"])
        try:
            phrase = machine_backup_passphrase()
            encrypt_backup(source, destination, phrase)
            with tempfile.TemporaryDirectory(prefix=".nexus-verify-", dir=destination.parent) as folder:
                verified = Path(folder) / "verified.dump"
                decrypt_backup(destination, verified, phrase)
                if digest(source) != digest(verified):
                    destination.unlink(missing_ok=True)
                    raise CommandError("فشل التحقق من تطابق النسخة بعد التشفير.")
            if options["delete_source"]:
                source.unlink()
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"Encrypted backup verified: {destination}")
        self.stdout.write(f"Encrypted size: {destination.stat().st_size}")