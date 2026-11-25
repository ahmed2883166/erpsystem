from django.core.management.base import BaseCommand, CommandError

from core.backup_key import machine_backup_passphrase


class Command(BaseCommand):
    help = "Verify the local Windows DPAPI backup encryption key can be opened."

    def handle(self, *args, **options):
        try:
            machine_backup_passphrase()
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write("Backup encryption key is ready.")