import django.core.validators
from decimal import Decimal
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0021_customer_receipt_batches"),
        ("treasury", "0009_treasury_fx_revaluation_reversal"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="CustomerRefund",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("refund_number", models.CharField(max_length=40)),
                ("refund_date", models.DateField()),
                ("amount", models.DecimalField(
                    decimal_places=4, max_digits=20,
                    validators=[django.core.validators.MinValueValidator(Decimal('0.0001'))],
                )),
                ("status", models.CharField(
                    choices=[("draft", "مسودة"), ("posted", "مرحّل"), ("reversed", "معكوس")],
                    default="draft", max_length=10,
                )),
                ("reversal_date", models.DateField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("branch", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="customer_refunds", to="companies.branch",
                )),
                ("company", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="customer_refunds", to="companies.company",
                )),
                ("created_by", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="created_customer_refunds", to=settings.AUTH_USER_MODEL,
                )),
                ("customer", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="refunds", to="customers.customer",
                )),
                ("reversal_journal_entry", models.OneToOneField(
                    blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="customer_refund_reversal", to="accounting.journalentry",
                )),
                ("reversal_treasury_transaction", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="reversed_customer_refunds", to="treasury.treasurytransaction",
                )),
                ("source_receipt_batch", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="refunds", to="sales.customerreceiptbatch",
                )),
                ("treasury_account", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="customer_refunds", to="treasury.treasuryaccount",
                )),
                ("treasury_transaction", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="customer_refunds", to="treasury.treasurytransaction",
                )),
            ],
            options={
                "ordering": ("-refund_date", "-id"),
                "indexes": [
                    models.Index(fields=["company", "status", "refund_date"], name="sales_refund_co_status_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(fields=("company", "refund_number"), name="uq_customer_refund_company_no"),
                ],
            },
        ),
    ]
