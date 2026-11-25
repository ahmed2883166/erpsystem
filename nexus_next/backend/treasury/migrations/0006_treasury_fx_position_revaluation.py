from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("companies", "0001_initial"),
        ("accounting", "0008_account_role_fx_roles"),
        ("treasury", "0005_merge_fx_and_opening_journal"),
    ]

    operations = [
        migrations.AddField(
            model_name="treasurytransaction",
            name="carrying_amount",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True),
        ),
        migrations.CreateModel(
            name="TreasuryFxPosition",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("foreign_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("carrying_base_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("account", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="fx_position", to="treasury.treasuryaccount")),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(condition=models.Q(("foreign_amount__gte", 0)), name="ck_treasury_fx_position_foreign_nonnegative"),
                    models.CheckConstraint(condition=models.Q(("carrying_base_amount__gte", 0)), name="ck_treasury_fx_position_base_nonnegative"),
                ],
            },
        ),
        migrations.CreateModel(
            name="TreasuryFxRevaluation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("revaluation_date", models.DateField()),
                ("rate_date", models.DateField()),
                ("rate", models.DecimalField(decimal_places=10, max_digits=20)),
                ("foreign_amount", models.DecimalField(decimal_places=4, max_digits=20)),
                ("previous_carrying_amount", models.DecimalField(decimal_places=4, max_digits=20)),
                ("new_carrying_amount", models.DecimalField(decimal_places=4, max_digits=20)),
                ("difference", models.DecimalField(decimal_places=4, max_digits=20)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("account", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="fx_revaluations", to="treasury.treasuryaccount")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="treasury_fx_revaluations", to="companies.company")),
                ("fiscal_year", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="treasury_fx_revaluations", to="companies.fiscalyear")),
                ("journal_entry", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="treasury_fx_revaluation", to="accounting.journalentry")),
            ],
            options={
                "ordering": ("-revaluation_date", "-id"),
                "constraints": [
                    models.UniqueConstraint(fields=("account", "revaluation_date"), name="uq_treasury_fx_revaluation_account_date"),
                    models.CheckConstraint(condition=models.Q(("rate__gt", 0)), name="ck_treasury_fx_revaluation_rate_positive"),
                    models.CheckConstraint(condition=models.Q(("foreign_amount__gte", 0)), name="ck_treasury_fx_revaluation_foreign_nonnegative"),
                ],
                "indexes": [
                    models.Index(fields=["company", "revaluation_date"], name="tr_fx_rev_co_date_idx"),
                ],
            },
        ),
    ]
