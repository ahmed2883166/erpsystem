from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0002_immutable_audit_trigger"),
    ]

    operations = [
        migrations.AlterField(
            model_name="auditevent",
            name="actor",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="audit_events", to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="auditevent",
            name="company",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="audit_events", to="companies.company",
            ),
        ),
        migrations.AlterField(
            model_name="auditevent",
            name="branch",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="audit_events", to="companies.branch",
            ),
        ),
    ]
