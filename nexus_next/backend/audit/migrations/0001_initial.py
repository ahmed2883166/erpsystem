import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("accounts", "0001_initial"),
        ("companies", "0003_companymembership_branch_scope"),
    ]

    operations = [
        migrations.CreateModel(
            name="AuditEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("request_id", models.UUIDField(db_index=True, default=uuid.uuid4, editable=False)),
                ("action", models.CharField(choices=[("create", "إنشاء"), ("update", "تعديل"), ("delete", "حذف"), ("action", "إجراء")], db_index=True, max_length=8)),
                ("resource", models.CharField(db_index=True, max_length=180)),
                ("object_id", models.CharField(blank=True, max_length=80)),
                ("http_method", models.CharField(max_length=8)),
                ("status_code", models.PositiveSmallIntegerField()),
                ("succeeded", models.BooleanField(db_index=True, default=False)),
                ("ip_address", models.GenericIPAddressField(blank=True, null=True)),
                ("user_agent", models.CharField(blank=True, max_length=300)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now, editable=False)),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="audit_events", to=settings.AUTH_USER_MODEL)),
                ("branch", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="audit_events", to="companies.branch")),
                ("company", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="audit_events", to="companies.company")),
            ],
            options={
                "verbose_name": "حدث تدقيق",
                "verbose_name_plural": "أحداث التدقيق",
                "ordering": ("-created_at", "-pk"),
            },
        ),
        migrations.AddIndex(model_name="auditevent", index=models.Index(fields=["company", "created_at"], name="audit_company_created_idx")),
        migrations.AddIndex(model_name="auditevent", index=models.Index(fields=["actor", "created_at"], name="audit_actor_created_idx")),
        migrations.AddIndex(model_name="auditevent", index=models.Index(fields=["resource", "created_at"], name="audit_resource_created_idx")),
    ]
