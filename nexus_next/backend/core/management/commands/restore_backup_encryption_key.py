from getpass import getpass
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.backup_key import default_backup_key_path, restore_backup_key


class Command(BaseCommand):
    help = "Restore the offline backup encryption key onto this Windows machine."

    def add_arguments(self, parser):
        parser.add_argument("--recovery-file", required=True)

    def handle(self, *args, **options):
        password = getpass("عبارة استرداد مفتاح النسخ الاحتياطي: ")
        try:
            path = restore_backup_key(
                Path(options["recovery_file"]), default_backup_key_path(), password
            )
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"تم استرداد مفتاح التشفير لهذا الجهاز: {path}"))