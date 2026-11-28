from django.conf import settings
from django.db import migrations, models
import django.core.validators
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0022_product_unit_weight_volume"),
        ("accounting", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="InventoryValuationAssessment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("assessment_number", models.CharField(max_length=40)),
                ("assessment_date", models.DateField()),
                ("net_realizable_value_per_unit", models.DecimalField(decimal_places=6, max_digits=20, validators=[django.core.validators.MinValueValidator(0)])),
                ("status", models.CharField(choices=[("draft", "مسودة"), ("posted", "مرحّل"), ("reversed", "معكوس")], default="draft", max_length=10)),
                ("quantity", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("carrying_value_before", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("carrying_value_after", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("adjustment_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("notes", models.TextField(blank=True)),
                ("posted_at", models.DateTimeField(blank=True, null=True)),
                ("reversed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="inventory_valuation_assessments", to="companies.company")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="created_inventory_valuation_assessments", to=settings.AUTH_USER_MODEL)),
                ("journal_entry", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="inventory_valuation_assessment", to="accounting.journalentry")),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="valuation_assessments", to="inventory.product")),
                ("reversal_journal_entry", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="inventory_valuation_assessment_reversal", to="accounting.journalentry")),
                ("posted_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="posted_inventory_valuation_assessments", to=settings.AUTH_USER_MODEL)),
                ("warehouse", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="valuation_assessments", to="inventory.warehouse")),
            ],
            options={"ordering": ("-assessment_date", "-id")},
        ),
        migrations.AddConstraint(model_name="inventoryvaluationassessment", constraint=models.UniqueConstraint(fields=("company", "assessment_number"), name="uq_inv_valuation_company_no")),
        migrations.AddIndex(model_name="inventoryvaluationassessment", index=models.Index(fields=["company", "warehouse", "product", "status"], name="inv_val_assess_scope_idx")),
        migrations.CreateModel(
            name="InventoryValuationAssessmentLine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quantity", models.DecimalField(decimal_places=4, max_digits=20)),
                ("original_average_cost", models.DecimalField(decimal_places=6, max_digits=20)),
                ("previous_average_cost", models.DecimalField(decimal_places=6, max_digits=20)),
                ("adjusted_average_cost", models.DecimalField(decimal_places=6, max_digits=20)),
                ("previous_inventory_value", models.DecimalField(decimal_places=4, max_digits=20)),
                ("adjusted_inventory_value", models.DecimalField(decimal_places=4, max_digits=20)),
                ("assessment", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="lines", to="inventory.inventoryvaluationassessment")),
                ("balance", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="valuation_assessment_lines", to="inventory.stockbalance")),
            ],
        ),
        migrations.AddConstraint(model_name="inventoryvaluationassessmentline", constraint=models.UniqueConstraint(fields=("assessment", "balance"), name="uq_inv_val_assess_balance")),
        migrations.CreateModel(
            name="InventoryValuationLayerAdjustment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("original_unit_cost", models.DecimalField(decimal_places=8, max_digits=20)),
                ("previous_unit_cost", models.DecimalField(decimal_places=8, max_digits=20)),
                ("adjusted_unit_cost", models.DecimalField(decimal_places=8, max_digits=20)),
                ("quantity_remaining", models.DecimalField(decimal_places=4, max_digits=20)),
                ("layer", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="valuation_adjustments", to="inventory.stockcostlayer")),
                ("line", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="layer_adjustments", to="inventory.inventoryvaluationassessmentline")),
            ],
        ),
        migrations.AddConstraint(model_name="inventoryvaluationlayeradjustment", constraint=models.UniqueConstraint(fields=("line", "layer"), name="uq_inv_val_line_layer")),
    ]
