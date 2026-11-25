from django.db import migrations


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

    IF NEW.status NOT IN ('approved', 'rejected')
       OR NEW.decided_by_id IS NULL
       OR NEW.decided_at IS NULL THEN
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

CREATE TRIGGER nexus_sales_invoice_approval_guard
BEFORE UPDATE OR DELETE ON approvals_salesinvoiceapproval
FOR EACH ROW EXECUTE FUNCTION nexus_guard_sales_invoice_approval();
"""

REVERSE_SQL = """
DROP TRIGGER IF EXISTS nexus_sales_invoice_approval_guard ON approvals_salesinvoiceapproval;
DROP FUNCTION IF EXISTS nexus_guard_sales_invoice_approval();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("approvals", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(FORWARD_SQL, REVERSE_SQL),
    ]
