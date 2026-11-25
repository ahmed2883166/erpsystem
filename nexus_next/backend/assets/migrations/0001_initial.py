import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = [
        ("accounting", "0003_inventory_account_roles"),
        ("companies", "0003_companymembership_branch_scope"),
    ]
    operations = [
        migrations.CreateModel(
            name="FixedAsset",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(max_length=40)),
                ("name_ar", models.CharField(max_length=180)),
                ("description", models.TextField(blank=True)),
                ("acquisition_date", models.DateField()),
                ("depreciation_start_date", models.DateField()),
                ("original_cost", models.DecimalField(decimal_places=4, max_digits=20)),
                ("salvage_value", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("useful_life_months", models.PositiveIntegerField()),
                ("depreciation_method", models.CharField(choices=[("straight_line", "القسط الثابت")], default="straight_line", max_length=20)),
                ("status", models.CharField(choices=[("active", "نشط"), ("disposed", "مستبعد")], default="active", max_length=10)),
                ("disposed_at", models.DateField(blank=True, null=True)),
                ("disposal_proceeds", models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("acquisition_counter_account", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="fixed_asset_acquisitions", to="accounting.chartofaccount")),
                ("acquisition_journal", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="acquired_fixed_asset", to="accounting.journalentry")),
                ("asset_account", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="fixed_asset_costs", to="accounting.chartofaccount")),
                ("accumulated_depreciation_account", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="fixed_asset_accumulated", to="accounting.chartofaccount")),
                ("branch", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="fixed_assets", to="companies.branch")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="fixed_assets", to="companies.company")),
                ("depreciation_expense_account", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="fixed_asset_expenses", to="accounting.chartofaccount")),
                ("disposal_gain_loss_account", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="fixed_asset_disposals", to="accounting.chartofaccount")),
                ("disposal_journal", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="disposed_fixed_asset", to="accounting.journalentry")),
            ],
            options={
                "ordering": ("code",),
                "constraints": [
                    models.UniqueConstraint(fields=("company", "code"), name="uq_asset_company_code"),
                    models.CheckConstraint(condition=models.Q(original_cost__gt=0), name="ck_asset_cost_positive"),
                    models.CheckConstraint(condition=models.Q(salvage_value__gte=0), name="ck_asset_salvage_nonnegative"),
                    models.CheckConstraint(condition=models.Q(useful_life_months__gt=0), name="ck_asset_life_positive"),
                ],
            },
        ),
        migrations.CreateModel(
            name="AssetDepreciation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("period_date", models.DateField()),
                ("amount", models.DecimalField(decimal_places=4, max_digits=20)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("asset", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="depreciations", to="assets.fixedasset")),
                ("journal_entry", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="asset_depreciation", to="accounting.journalentry")),
            ],
            options={
                "ordering": ("period_date",),
                "constraints": [models.UniqueConstraint(fields=("asset", "period_date"), name="uq_asset_depr_period")],
            },
        ),
    ]
