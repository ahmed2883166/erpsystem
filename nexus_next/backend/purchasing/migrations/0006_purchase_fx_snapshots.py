import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("companies", "0003_companymembership_branch_scope"),
        ("purchasing", "0005_supplier_payment_account"),
    ]

    operations = [
        migrations.AddField(model_name="purchaseinvoice", name="transaction_currency",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="purchase_invoices", to="companies.currency")),
        migrations.AddField(model_name="purchaseinvoice", name="exchange_rate",
            field=models.DecimalField(blank=True, decimal_places=10, max_digits=20, null=True)),
        migrations.AddField(model_name="purchaseinvoice", name="exchange_rate_date",
            field=models.DateField(blank=True, null=True)),
        migrations.AddField(model_name="purchaseinvoice", name="transaction_net_total",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True)),
        migrations.AddField(model_name="purchaseinvoice", name="transaction_tax_total",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True)),
        migrations.AddField(model_name="purchaseinvoice", name="transaction_grand_total",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True)),
        migrations.AddField(model_name="purchaseinvoiceline", name="transaction_unit_price",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=18, null=True, validators=[django.core.validators.MinValueValidator(0)])),
        migrations.AddField(model_name="purchaseinvoiceline", name="transaction_net_amount",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True)),
        migrations.AddField(model_name="purchaseinvoiceline", name="transaction_tax_amount",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True)),
        migrations.AddField(model_name="purchaseinvoiceline", name="transaction_total_amount",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True)),
        migrations.AddField(model_name="supplierpayment", name="transaction_currency",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="supplier_payments", to="companies.currency")),
        migrations.AddField(model_name="supplierpayment", name="transaction_amount",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True)),
        migrations.AddField(model_name="supplierpayment", name="exchange_rate",
            field=models.DecimalField(blank=True, decimal_places=10, max_digits=20, null=True)),
        migrations.AddField(model_name="supplierpayment", name="exchange_rate_date",
            field=models.DateField(blank=True, null=True)),
        migrations.AddField(model_name="supplierpayment", name="carrying_base_amount",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True)),
        migrations.AddField(model_name="supplierpayment", name="realized_fx_gain",
            field=models.DecimalField(decimal_places=4, default=0, max_digits=20)),
        migrations.AddField(model_name="supplierpayment", name="realized_fx_loss",
            field=models.DecimalField(decimal_places=4, default=0, max_digits=20)),
    ]
