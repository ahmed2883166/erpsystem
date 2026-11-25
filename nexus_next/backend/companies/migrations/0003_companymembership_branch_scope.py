from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("companies", "0002_companymembership"),
    ]

    operations = [
        migrations.AddField(
            model_name="companymembership",
            name="all_branches",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="companymembership",
            name="branches",
            field=models.ManyToManyField(blank=True, related_name="user_memberships", to="companies.branch"),
        ),
    ]
