from django.db import migrations


FORWARD_SQL = """
CREATE OR REPLACE FUNCTION nexus_guard_financial_approval_request()
RETURNS trigger AS $$
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
$$ LANGUAGE plpgsql;

CREATE TRIGGER nexus_financial_approval_request_guard
BEFORE UPDATE OR DELETE ON approvals_financialapprovalrequest
FOR EACH ROW EXECUTE FUNCTION nexus_guard_financial_approval_request();
"""

REVERSE_SQL = """
DROP TRIGGER IF EXISTS nexus_financial_approval_request_guard ON approvals_financialapprovalrequest;
DROP FUNCTION IF EXISTS nexus_guard_financial_approval_request();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("approvals", "0011_purchase_payment_approvals"),
    ]

    operations = [
        migrations.RunSQL(FORWARD_SQL, REVERSE_SQL),
    ]
