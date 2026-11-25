from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_backupconfiguration"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(
            name="BackupRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("success", "ناجح"), ("failure", "فشل")], db_index=True, max_length=10)),
                ("backup_file", models.CharField(blank=True, max_length=500)),
                ("size_bytes", models.BigIntegerField(default=0)),
                ("details", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("started_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("-created_at", "-pk"), "verbose_name": "عملية نسخ احتياطي", "verbose_name_plural": "عمليات النسخ الاحتياطي"},
        ),
    ]
