import django.core.validators
import django.db.models.deletion
from django.db import migrations, models
from decimal import Decimal


class Migration(migrations.Migration):
    dependencies = [
        ("purchasing", "0023_purchase_invoice_service_acceptance"),
    ]

    operations = [
        migrations.CreateModel(
            name="LandedCostDocument",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("document_number", models.CharField(max_length=40)),
                ("document_date", models.DateField()),
                ("status", models.CharField(choices=[("draft", "مسودة"), ("posted", "مرحّل"), ("reversed", "معكوس")], default="draft", max_length=10)),
                ("total_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("notes", models.TextField(blank=True)),
                ("posted_at", models.DateTimeField(blank=True, null=True)),
                ("reversed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="landed_cost_documents", to="companies.company")),
                ("source_invoice", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="landed_cost_documents", to="purchasing.purchaseinvoice")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="created_landed_cost_documents", to="accounts.user")),
                ("posted_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="posted_landed_cost_documents", to="accounts.user")),
                ("reversal_journal_entry", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="landed_cost_document_reversals", to="accounting.journalentry")),
            ],
            options={
                "ordering": ("-document_date", "-id"),
                "indexes": [models.Index(fields=["company", "status", "document_date"], name="pur_lcost_status_date_idx")],
                "constraints": [
                    models.UniqueConstraint(fields=("company", "document_number"), name="uq_landed_cost_company_no"),
                    models.CheckConstraint(condition=models.Q(total_amount__gte=0), name="ck_landed_cost_total_nonnegative"),
                ],
            },
        ),
        migrations.CreateModel(
            name="LandedCostAllocation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("basis", models.CharField(choices=[("value", "القيمة"), ("quantity", "الكمية"), ("weight", "الوزن"), ("volume", "الحجم"), ("manual", "توزيع يدوي")], default="value", max_length=12)),
                ("amount", models.DecimalField(decimal_places=4, max_digits=20, validators=[django.core.validators.MinValueValidator(Decimal("0.0001"))])),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("document", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="allocations", to="purchasing.landedcostdocument")),
                ("source_invoice_line", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="landed_cost_allocations", to="purchasing.purchaseinvoiceline")),
                ("receipt_line", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="landed_cost_allocations", to="inventory.stockmovementline")),
            ],
            options={
                "ordering": ("id",),
                "constraints": [
                    models.UniqueConstraint(fields=("document", "source_invoice_line", "receipt_line"), name="uq_lcost_doc_expense_receipt"),
                    models.CheckConstraint(condition=models.Q(amount__gt=0), name="ck_lcost_allocation_positive"),
                ],
            },
        ),
        migrations.CreateModel(
            name="LandedCostDistribution",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("inventory_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("cogs_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("adjustment_loss_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("allocation", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="distributions", to="purchasing.landedcostallocation")),
                ("branch", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="landed_cost_distributions", to="companies.branch")),
                ("journal_entry", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="landed_cost_distribution", to="accounting.journalentry")),
            ],
            options={
                "ordering": ("branch_id", "id"),
                "constraints": [
                    models.UniqueConstraint(fields=("allocation", "branch"), name="uq_lcost_allocation_branch"),
                    models.CheckConstraint(condition=models.Q(inventory_amount__gte=0), name="ck_lcost_dist_inventory_nonnegative"),
                    models.CheckConstraint(condition=models.Q(cogs_amount__gte=0), name="ck_lcost_dist_cogs_nonnegative"),
                    models.CheckConstraint(condition=models.Q(adjustment_loss_amount__gte=0), name="ck_lcost_dist_loss_nonnegative"),
                ],
            },
        ),
    ]