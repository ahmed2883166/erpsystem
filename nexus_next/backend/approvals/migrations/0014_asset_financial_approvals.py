import django.db.models.deletion
from django.db import migrations, models



FORWARD_SQL = """
CREATE OR REPLACE FUNCTION nexus_guard_financial_approval_request()
RETURNS trigger AS $
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'NEXUS financial approval requests cannot be deleted';
    END IF;
    IF OLD.status <> 'pending' THEN
        RAISE EXCEPTION 'NEXUS financial approval decisions are final';
    END IF;
    IF NEW.status = 'superseded' THEN
        IF NEW.decided_by_id IS NOT NULL OR NEW.decided_at IS NULL THEN
            RAISE EXCEPTION 'NEXUS superseded financial approval state is invalid';
        END IF;
    ELSIF NEW.status IN ('approved', 'rejected') THEN
        IF NEW.decided_by_id IS NULL OR NEW.decided_at IS NULL THEN
            RAISE EXCEPTION 'NEXUS financial approval decision requires an actor and timestamp';
        END IF;
    ELSE
        RAISE EXCEPTION 'NEXUS financial approval transition is invalid';
    END IF;
    IF NEW.company_id IS DISTINCT FROM OLD.company_id
       OR NEW.document_type IS DISTINCT FROM OLD.document_type
       OR NEW.purchase_invoice_id IS DISTINCT FROM OLD.purchase_invoice_id
       OR NEW.supplier_payment_id IS DISTINCT FROM OLD.supplier_payment_id
       OR NEW.treasury_transaction_id IS DISTINCT FROM OLD.treasury_transaction_id
       OR NEW.asset_purchase_invoice_id IS DISTINCT FROM OLD.asset_purchase_invoice_id
       OR NEW.asset_supplier_payment_id IS DISTINCT FROM OLD.asset_supplier_payment_id
       OR NEW.requested_by_id IS DISTINCT FROM OLD.requested_by_id
       OR NEW.requested_limit IS DISTINCT FROM OLD.requested_limit
       OR NEW.requested_amount IS DISTINCT FROM OLD.requested_amount
       OR NEW.requested_snapshot IS DISTINCT FROM OLD.requested_snapshot
       OR NEW.requested_at IS DISTINCT FROM OLD.requested_at THEN
        RAISE EXCEPTION 'NEXUS financial approval request identity and snapshot are immutable';
    END IF;
    RETURN NEW;
END;
$ LANGUAGE plpgsql;
"""

REVERSE_SQL = """
CREATE OR REPLACE FUNCTION nexus_guard_financial_approval_request()
RETURNS trigger AS $
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'NEXUS financial approval requests cannot be deleted';
    END IF;
    IF OLD.status <> 'pending' THEN
        RAISE EXCEPTION 'NEXUS financial approval decisions are final';
    END IF;
    IF NEW.status = 'superseded' THEN
        IF NEW.decided_by_id IS NOT NULL OR NEW.decided_at IS NULL THEN
            RAISE EXCEPTION 'NEXUS superseded financial approval state is invalid';
        END IF;
    ELSIF NEW.status IN ('approved', 'rejected') THEN
        IF NEW.decided_by_id IS NULL OR NEW.decided_at IS NULL THEN
            RAISE EXCEPTION 'NEXUS financial approval decision requires an actor and timestamp';
        END IF;
    ELSE
        RAISE EXCEPTION 'NEXUS financial approval transition is invalid';
    END IF;
    IF NEW.company_id IS DISTINCT FROM OLD.company_id
       OR NEW.document_type IS DISTINCT FROM OLD.document_type
       OR NEW.purchase_invoice_id IS DISTINCT FROM OLD.purchase_invoice_id
       OR NEW.supplier_payment_id IS DISTINCT FROM OLD.supplier_payment_id
       OR NEW.treasury_transaction_id IS DISTINCT FROM OLD.treasury_transaction_id
       OR NEW.requested_by_id IS DISTINCT FROM OLD.requested_by_id
       OR NEW.requested_limit IS DISTINCT FROM OLD.requested_limit
       OR NEW.requested_amount IS DISTINCT FROM OLD.requested_amount
       OR NEW.requested_snapshot IS DISTINCT FROM OLD.requested_snapshot
       OR NEW.requested_at IS DISTINCT FROM OLD.requested_at THEN
        RAISE EXCEPTION 'NEXUS financial approval request identity and snapshot are immutable';
    END IF;
    RETURN NEW;
END;
$ LANGUAGE plpgsql;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("approvals", "0013_sales_approval_supersede_guard"),
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
        migrations.RunSQL(FORWARD_SQL, REVERSE_SQL),
    ]
