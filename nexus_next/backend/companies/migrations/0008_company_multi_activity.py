from django.db import migrations, models
import django.db.models.deletion


def backfill_primary_activities(apps, schema_editor):
    Company = apps.get_model("companies", "Company")
    CompanyActivity = apps.get_model("companies", "CompanyActivity")
    CompanyActivity.objects.bulk_create(
        [
            CompanyActivity(company_id=company_id, activity=activity)
            for company_id, activity in Company.objects.values_list("id", "activity")
        ],
        ignore_conflicts=True,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("companies", "0007_seed_default_currencies"),
    ]

    operations = [
        migrations.CreateModel(
            name="CompanyActivity",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("activity", models.CharField(
                    choices=[
                        ("commercial", "تجاري"),
                        ("service", "خدمي"),
                        ("medical", "طبي"),
                        ("restaurants", "مطاعم"),
                        ("industrial", "صناعي"),
                            ],
                    max_length=24,
                )),
                ("is_active", models.BooleanField(default=True)),
                ("company", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="activity_assignments",
                    to="companies.company",
                )),
            ],
            options={
                "verbose_name": "نشاط شركة",
                "verbose_name_plural": "أنشطة الشركات",
                "ordering": ("activity",),
            },
        ),
        migrations.AddIndex(
            model_name="companyactivity",
            index=models.Index(fields=["company", "is_active"], name="company_activity_active_idx"),
        ),
        migrations.AddConstraint(
            model_name="companyactivity",
            constraint=models.UniqueConstraint(
                fields=("company", "activity"), name="uq_company_enabled_activity"
            ),
        ),
        migrations.RunPython(backfill_primary_activities, migrations.RunPython.noop),
    ]
