from django.core.management.base import BaseCommand, CommandError
from core.models import BackupConfiguration


class Command(BaseCommand):
    help = "Print the configured PostgreSQL backup destination for the local backup runner."

    def handle(self, *args, **options):
        config = BackupConfiguration.objects.first()
        if not config or not config.enabled or not config.backup_path:
            raise CommandError("PostgreSQL backup is disabled or its destination is not configured.")
        self.stdout.write(config.backup_path)
