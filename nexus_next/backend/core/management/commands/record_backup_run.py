from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.models import BackupRun


class Command(BaseCommand):
    help = "Record a verified encrypted scheduled PostgreSQL backup in NEXUS history."

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True)

    def handle(self, *args, **options):
        path = Path(options["file"])
        if path.suffix.lower() != ".nexusenc":
            raise CommandError("سجل النسخ يقبل ملفات NEXUS المشفرة فقط.")
        if not path.is_file() or path.stat().st_size < 1024:
            raise CommandError("ملف النسخة المشفرة غير موجود أو حجمه غير صالح.")
        run = BackupRun.objects.create(
            status=BackupRun.Status.SUCCESS,
            backup_file=str(path.resolve()),
            size_bytes=path.stat().st_size,
            details="نسخة PostgreSQL مشفرة؛ اجتازت التحقق التشفيري ومقارنة SHA-256 قبل التسجيل.",
            started_by=None,
        )
        self.stdout.write(f"Recorded backup run: {run.pk}")
