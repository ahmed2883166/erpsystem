from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.backup_crypto import decrypt_backup
from core.backup_key import machine_backup_passphrase


class Command(BaseCommand):
    help = "Decrypt a NEXUS backup using the local Windows DPAPI key."

    def add_arguments(self, parser):
        parser.add_argument("--input", required=True)
        parser.add_argument("--output", required=True)

    def handle(self, *args, **options):
        try:
            output = decrypt_backup(
                Path(options["input"]), Path(options["output"]), machine_backup_passphrase()
            )
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"Decrypted backup verified: {output}")