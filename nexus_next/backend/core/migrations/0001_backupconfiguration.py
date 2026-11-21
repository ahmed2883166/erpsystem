from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.core.validators


class Migration(migrations.Migration):
    initial = True
    dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(
            name="BackupConfiguration",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("backup_path", models.CharField(blank=True, max_length=500)),
                ("schedule", models.CharField(choices=[("daily", "يومي"), ("weekly", "أسبوعي"), ("monthly", "شهري")], default="daily", max_length=10)),
                ("enabled", models.BooleanField(default=True)),
                ("daily_retention_days", models.PositiveSmallIntegerField(default=30, validators=[django.core.validators.MinValueValidator(1)])),
                ("monthly_retention_months", models.PositiveSmallIntegerField(default=12, validators=[django.core.validators.MinValueValidator(1)])),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("updated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={"verbose_name": "إعدادات النسخ الاحتياطي", "verbose_name_plural": "إعدادات النسخ الاحتياطي"},
        ),
    ]
