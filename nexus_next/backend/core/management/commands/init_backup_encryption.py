from getpass import getpass
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.backup_key import default_backup_key_path, initialize_backup_key


class Command(BaseCommand):
    help = "Initialize unattended Windows backup encryption and create an offline recovery file."

    def add_arguments(self, parser):
        parser.add_argument("--recovery-file", required=True, help="Encrypted recovery file path, preferably on an offline USB drive.")

    def handle(self, *args, **options):
        first = getpass("عبارة استرداد النسخ الاحتياطي (12 حرفًا على الأقل): ")
        second = getpass("أعد إدخال عبارة الاسترداد: ")
        if first != second:
            raise CommandError("عبارتا المرور غير متطابقتين.")
        try:
            key_file, recovery_file = initialize_backup_key(
                default_backup_key_path(), Path(options["recovery_file"]), first
            )
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS("تم تهيئة تشفير النسخ التلقائي وحفظ نسخة الاسترداد المشفرة."))
        self.stdout.write(f"Local DPAPI key: {key_file}")
        self.stdout.write(f"Offline recovery file: {recovery_file}")
        self.stdout.write("احتفظ بملف الاسترداد على USB منفصل وبعبارة المرور في مكان آمن منفصل.")