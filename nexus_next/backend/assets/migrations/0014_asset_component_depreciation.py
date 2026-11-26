from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("assets", "0013_asset_components_inventory_insurance_methods"),
        ("accounting", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="AssetComponentDepreciation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("period_date", models.DateField()),
                ("amount", models.DecimalField(decimal_places=4, max_digits=20)),
                ("units_produced", models.PositiveBigIntegerField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("component", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="depreciations", to="assets.assetcomponent")),
                ("journal_entry", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="asset_component_depreciations", to="accounting.journalentry")),
            ],
            options={"ordering": ("period_date", "component_id")},
        ),
        migrations.AddConstraint(
            model_name="assetcomponentdepreciation",
            constraint=models.UniqueConstraint(fields=("component", "period_date"), name="uq_asset_component_depr_period"),
        ),
        migrations.AddConstraint(
            model_name="assetcomponentdepreciation",
            constraint=models.CheckConstraint(condition=models.Q(("amount__gt", 0)), name="ck_asset_component_depr_positive"),
        ),
    ]
