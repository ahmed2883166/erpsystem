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
"""


class Migration(migrations.Migration):
    dependencies = [
        ("approvals", "0012_financial_approval_immutability"),
    ]

    operations = [
        migrations.RunSQL(FORWARD_SQL, REVERSE_SQL),
    ]
