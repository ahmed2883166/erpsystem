from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("companies", "0003_companymembership_branch_scope"),
        ("inventory", "0008_fifo_cost_lineage"),
        ("purchasing", "0008_supplier_payment_reversed_status"),
    ]

    operations = [
        migrations.CreateModel(
            name="PurchaseInvoiceTaxAllocation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("inventory_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("cogs_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("adjustment_loss_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("branch", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="purchase_tax_allocations", to="companies.branch")),
                ("invoice", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="tax_allocations", to="purchasing.purchaseinvoice")),
                ("journal_entry", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="purchase_invoice_tax_allocation", to="accounting.journalentry")),
            ],
            options={"ordering": ("branch_id", "id")},
        ),
        migrations.AddConstraint(
            model_name="purchaseinvoicetaxallocation",
            constraint=models.UniqueConstraint(fields=("invoice", "branch"), name="uq_purchase_tax_allocation_branch"),
        ),
    ]
