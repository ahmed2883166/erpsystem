# Generated for direct service and expense supplier invoices.
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0011_ledger_reporting_indexes"),
        ("companies", "0001_initial"),
        ("purchasing", "0019_partial_purchase_invoices"),
    ]

    operations = [
        migrations.AlterField(
            model_name="purchaseinvoice",
            name="receipt",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="invoices", to="purchasing.purchasereceipt"),
        ),
        migrations.AddField(
            model_name="purchaseinvoice",
            name="invoice_type",
            field=models.CharField(choices=[("inventory", "مشتريات مخزنية"), ("direct_expense", "خدمات ومصروفات مباشرة")], default="inventory", max_length=20),
        ),
        migrations.AddField(
            model_name="purchaseinvoice",
            name="branch",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="purchase_invoices", to="companies.branch"),
        ),
        migrations.AlterField(
            model_name="purchaseinvoiceline",
            name="receipt_line",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="purchase_invoice_lines", to="inventory.stockmovementline"),
        ),
        migrations.AddField(
            model_name="purchaseinvoiceline",
            name="description",
            field=models.CharField(blank=True, max_length=240),
        ),
        migrations.AddField(
            model_name="purchaseinvoiceline",
            name="expense_account",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="purchase_expense_lines", to="accounting.chartofaccount"),
        ),
        migrations.AddField(
            model_name="purchaseinvoiceline",
            name="cost_center",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="purchase_invoice_lines", to="accounting.costcenter"),
        ),
    ]
