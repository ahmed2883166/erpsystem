from django.db import migrations, models


FORWARD_SQL = """
CREATE OR REPLACE FUNCTION nexus_guard_sales_invoice_approval()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'NEXUS approval requests cannot be deleted';
    END IF;
    IF OLD.status <> 'pending' THEN
        RAISE EXCEPTION 'NEXUS approval decisions are final';
    END IF;
    IF NEW.status = 'superseded' THEN
        IF NEW.decided_by_id IS NOT NULL OR NEW.decided_at IS NULL THEN
            RAISE EXCEPTION 'NEXUS superseded approval state is invalid';
        END IF;
    ELSIF NEW.status IN ('approved', 'rejected') THEN
        IF NEW.decided_by_id IS NULL OR NEW.decided_at IS NULL THEN
            RAISE EXCEPTION 'NEXUS approval decision requires an actor and timestamp';
        END IF;
    ELSE
        RAISE EXCEPTION 'NEXUS approval transition is invalid';
    END IF;
    IF NEW.company_id IS DISTINCT FROM OLD.company_id
       OR NEW.invoice_id IS DISTINCT FROM OLD.invoice_id
       OR NEW.requested_by_id IS DISTINCT FROM OLD.requested_by_id
       OR NEW.requested_limit IS DISTINCT FROM OLD.requested_limit
       OR NEW.requested_invoice_limit IS DISTINCT FROM OLD.requested_invoice_limit
       OR NEW.requested_discount_total IS DISTINCT FROM OLD.requested_discount_total
       OR NEW.requested_max_discount_percent IS DISTINCT FROM OLD.requested_max_discount_percent
       OR NEW.requested_credit_limit IS DISTINCT FROM OLD.requested_credit_limit
       OR NEW.requested_credit_exposure IS DISTINCT FROM OLD.requested_credit_exposure
       OR NEW.requested_credit_amount IS DISTINCT FROM OLD.requested_credit_amount
       OR NEW.requested_lines_snapshot IS DISTINCT FROM OLD.requested_lines_snapshot
       OR NEW.requested_at IS DISTINCT FROM OLD.requested_at THEN
        RAISE EXCEPTION 'NEXUS approval request identity and snapshot are immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

REVERSE_SQL = """
CREATE OR REPLACE FUNCTION nexus_guard_sales_invoice_approval()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'NEXUS approval requests cannot be deleted';
    END IF;
    IF OLD.status <> 'pending' THEN
        RAISE EXCEPTION 'NEXUS approval decisions are final';
    END IF;
    IF NEW.status NOT IN ('approved', 'rejected')
       OR NEW.decided_by_id IS NULL OR NEW.decided_at IS NULL THEN
        RAISE EXCEPTION 'NEXUS approval transition is invalid';
    END IF;
    IF NEW.company_id IS DISTINCT FROM OLD.company_id
       OR NEW.invoice_id IS DISTINCT FROM OLD.invoice_id
       OR NEW.requested_by_id IS DISTINCT FROM OLD.requested_by_id
       OR NEW.requested_limit IS DISTINCT FROM OLD.requested_limit
       OR NEW.requested_at IS DISTINCT FROM OLD.requested_at THEN
        RAISE EXCEPTION 'NEXUS approval request identity is immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def update_approval_guard(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(FORWARD_SQL)


def restore_approval_guard(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(REVERSE_SQL)


class Migration(migrations.Migration):
    dependencies = [
        ("approvals", "0016_purchase_order_approval"),
    ]

    operations = [
        migrations.AddField(
            model_name="salesinvoiceapproval",
            name="requested_credit_limit",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True),
        ),
        migrations.AddField(
            model_name="salesinvoiceapproval",
            name="requested_credit_exposure",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True),
        ),
        migrations.AddField(
            model_name="salesinvoiceapproval",
            name="requested_credit_amount",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True),
        ),
        migrations.RunPython(update_approval_guard, restore_approval_guard),
    ]
