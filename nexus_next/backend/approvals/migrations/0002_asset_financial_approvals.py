import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("approvals", "0001_initial"),
        ("assets", "0008_asset_vendor_invoice_and_payments"),
    ]

    operations = [
        migrations.AddField(
            model_name="financialapprovalrequest",
            name="asset_purchase_invoice",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="approval_requests", to="assets.assetpurchaseinvoice"),
        ),
        migrations.AddField(
            model_name="financialapprovalrequest",
            name="asset_supplier_payment",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="approval_requests", to="assets.assetsupplierpayment"),
        ),
        migrations.RemoveConstraint(
            model_name="financialapprovalrequest",
            name="financial_approval_one_document",
        ),
        migrations.AddConstraint(
            model_name="financialapprovalrequest",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(document_type="purchase_invoice", purchase_invoice__isnull=False, supplier_payment__isnull=True, treasury_transaction__isnull=True, asset_purchase_invoice__isnull=True, asset_supplier_payment__isnull=True)
                    | models.Q(document_type="supplier_payment", purchase_invoice__isnull=True, supplier_payment__isnull=False, treasury_transaction__isnull=True, asset_purchase_invoice__isnull=True, asset_supplier_payment__isnull=True)
                    | models.Q(document_type="treasury_payment", purchase_invoice__isnull=True, supplier_payment__isnull=True, treasury_transaction__isnull=False, asset_purchase_invoice__isnull=True, asset_supplier_payment__isnull=True)
                    | models.Q(document_type="asset_purchase_invoice", purchase_invoice__isnull=True, supplier_payment__isnull=True, treasury_transaction__isnull=True, asset_purchase_invoice__isnull=False, asset_supplier_payment__isnull=True)
                    | models.Q(document_type="asset_supplier_payment", purchase_invoice__isnull=True, supplier_payment__isnull=True, treasury_transaction__isnull=True, asset_purchase_invoice__isnull=True, asset_supplier_payment__isnull=False)
                ),
                name="financial_approval_one_document",
            ),
        ),
    ]
