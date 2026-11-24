from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0011_ledger_reporting_indexes"),
        ("assets", "0009_recoverable_acquisition_tax"),
    ]

    operations = [
        migrations.CreateModel(
            name="AssetImpairment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("impairment_date", models.DateField()),
                ("carrying_amount_before", models.DecimalField(decimal_places=4, max_digits=20)),
                ("recoverable_amount", models.DecimalField(decimal_places=4, max_digits=20)),
                ("impairment_loss", models.DecimalField(decimal_places=4, max_digits=20)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("accumulated_impairment_account", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="fixed_asset_accumulated_impairments", to="accounting.chartofaccount",
                )),
                ("asset", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="impairments", to="assets.fixedasset",
                )),
                ("impairment_expense_account", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="fixed_asset_impairment_expenses", to="accounting.chartofaccount",
                )),
                ("journal_entry", models.OneToOneField(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="asset_impairment", to="accounting.journalentry",
                )),
            ],
            options={"ordering": ("impairment_date", "id")},
        ),
        migrations.AddConstraint(
            model_name="assetimpairment",
            constraint=models.UniqueConstraint(fields=("asset", "impairment_date"), name="uq_asset_impairment_date"),
        ),
        migrations.AddConstraint(
            model_name="assetimpairment",
            constraint=models.CheckConstraint(condition=models.Q(("carrying_amount_before__gt", 0)), name="ck_asset_impairment_carrying_positive"),
        ),
        migrations.AddConstraint(
            model_name="assetimpairment",
            constraint=models.CheckConstraint(condition=models.Q(("recoverable_amount__gte", 0)), name="ck_asset_impairment_recoverable_nonnegative"),
        ),
        migrations.AddConstraint(
            model_name="assetimpairment",
            constraint=models.CheckConstraint(condition=models.Q(("impairment_loss__gt", 0)), name="ck_asset_impairment_loss_positive"),
        ),
    ]
