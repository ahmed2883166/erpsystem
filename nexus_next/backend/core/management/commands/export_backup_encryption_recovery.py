from getpass import getpass
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.backup_crypto import encrypt_recovery_key
from core.backup_key import machine_backup_key


class Command(BaseCommand):
    help = "Create a replacement encrypted USB recovery file from the local DPAPI key."

    def add_arguments(self, parser):
        parser.add_argument("--output", required=True)

    def handle(self, *args, **options):
        first = getpass("عبارة مرور ملف الاسترداد الجديد (12 حرفًا على الأقل): ")
        second = getpass("أعد إدخال عبارة المرور: ")
        if first != second:
            raise CommandError("عبارتا المرور غير متطابقتين.")
        try:
            key = machine_backup_key()
            path = encrypt_recovery_key(key, Path(options["output"]), first)
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"تم إنشاء ملف استرداد جديد: {path}"))
        self.stdout.write("احتفظ بالملف الجديد على USB منفصل واختبره قبل إتلاف أي نسخة استرداد قديمة.")
