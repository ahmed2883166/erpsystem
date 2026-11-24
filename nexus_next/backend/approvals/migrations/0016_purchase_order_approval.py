import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("approvals", "0015_alter_financialapprovalrequest_document_type"),
        ("purchasing", "0017_purchase_rfq_order_conversion"),
    ]

    operations = [
        migrations.AddField(
            model_name="discountapprovalpolicy",
            name="max_purchase_order_amount",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True, validators=[django.core.validators.MinValueValidator(0)], help_text="أقصى إجمالي أمر شراء دون موافقة مدير الشركة."),
        ),
        migrations.RemoveConstraint(
            model_name="discountapprovalpolicy",
            name="discount_policy_has_limit",
        ),
        migrations.AddConstraint(
            model_name="discountapprovalpolicy",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(max_discount_percent__isnull=False)
                    | models.Q(max_sales_invoice_amount__isnull=False)
                    | models.Q(max_purchase_invoice_amount__isnull=False)
                    | models.Q(max_purchase_order_amount__isnull=False)
                    | models.Q(max_payment_amount__isnull=False)
                ),
                name="discount_policy_has_limit",
            ),
        ),
        migrations.AddField(
            model_name="financialapprovalrequest",
            name="purchase_order",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="approval_requests", to="purchasing.purchaseorder"),
        ),
        migrations.AlterField(
            model_name="financialapprovalrequest",
            name="document_type",
            field=models.CharField(choices=[("purchase_invoice", "فاتورة مشتريات"), ("purchase_order", "أمر شراء"), ("supplier_payment", "سند صرف مورد"), ("treasury_payment", "سند صرف خزينة"), ("asset_purchase_invoice", "فاتورة شراء أصل ثابت"), ("asset_supplier_payment", "سند سداد أصل ثابت")], max_length=24),
        ),
        migrations.RemoveConstraint(
            model_name="financialapprovalrequest",
            name="financial_approval_one_document",
        ),
        migrations.AddConstraint(
            model_name="financialapprovalrequest",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(document_type="purchase_invoice", purchase_invoice__isnull=False, purchase_order__isnull=True, supplier_payment__isnull=True, treasury_transaction__isnull=True, asset_purchase_invoice__isnull=True, asset_supplier_payment__isnull=True)
                    | models.Q(document_type="purchase_order", purchase_invoice__isnull=True, purchase_order__isnull=False, supplier_payment__isnull=True, treasury_transaction__isnull=True, asset_purchase_invoice__isnull=True, asset_supplier_payment__isnull=True)
                    | models.Q(document_type="supplier_payment", purchase_invoice__isnull=True, purchase_order__isnull=True, supplier_payment__isnull=False, treasury_transaction__isnull=True, asset_purchase_invoice__isnull=True, asset_supplier_payment__isnull=True)
                    | models.Q(document_type="treasury_payment", purchase_invoice__isnull=True, purchase_order__isnull=True, supplier_payment__isnull=True, treasury_transaction__isnull=False, asset_purchase_invoice__isnull=True, asset_supplier_payment__isnull=True)
                    | models.Q(document_type="asset_purchase_invoice", purchase_invoice__isnull=True, purchase_order__isnull=True, supplier_payment__isnull=True, treasury_transaction__isnull=True, asset_purchase_invoice__isnull=False, asset_supplier_payment__isnull=True)
                    | models.Q(document_type="asset_supplier_payment", purchase_invoice__isnull=True, purchase_order__isnull=True, supplier_payment__isnull=True, treasury_transaction__isnull=True, asset_purchase_invoice__isnull=True, asset_supplier_payment__isnull=False)
                ),
                name="financial_approval_one_document",
            ),
        ),
    ]
