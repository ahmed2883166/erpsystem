from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0020_interbranch_stock_transfers"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="WarehouseStatusAudit",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("previous_is_active", models.BooleanField()),
                ("new_is_active", models.BooleanField()),
                ("reason", models.TextField(blank=True)),
                ("changed_at", models.DateTimeField(auto_now_add=True)),
                ("changed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="warehouse_status_audits", to=settings.AUTH_USER_MODEL)),
                ("warehouse", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="status_audit_entries", to="inventory.warehouse")),
            ],
            options={"ordering": ("-changed_at", "-pk")},
        ),
        migrations.AddIndex(
            model_name="warehousestatusaudit",
            index=models.Index(fields=["warehouse", "-changed_at"], name="wh_status_audit_idx"),
        ),
    ]
