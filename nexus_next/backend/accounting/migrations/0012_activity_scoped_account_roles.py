import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0011_ledger_reporting_indexes"),
        ("companies", "0008_company_multi_activity"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="accountrole",
            name="uq_account_role_company",
        ),
        migrations.AddField(
            model_name="accountrole",
            name="activity",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="account_role_assignments", to="companies.companyactivity",
            ),
        ),
        migrations.AddConstraint(
            model_name="accountrole",
            constraint=models.UniqueConstraint(
                condition=Q(activity__isnull=True), fields=("company", "role"),
                name="uq_account_role_company",
            ),
        ),
        migrations.AddConstraint(
            model_name="accountrole",
            constraint=models.UniqueConstraint(
                condition=Q(activity__isnull=False), fields=("company", "role", "activity"),
                name="uq_acct_role_co_activity",
            ),
        ),
    ]
