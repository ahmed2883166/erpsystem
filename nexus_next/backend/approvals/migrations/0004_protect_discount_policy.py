from django.db import migrations


FORWARD_SQL = """
CREATE OR REPLACE FUNCTION nexus_protect_discount_approval_policy()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'NEXUS discount approval policies cannot be deleted; update the limit instead';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER nexus_discount_approval_policy_no_delete
BEFORE DELETE ON approvals_discountapprovalpolicy
FOR EACH ROW EXECUTE FUNCTION nexus_protect_discount_approval_policy();
"""

REVERSE_SQL = """
DROP TRIGGER IF EXISTS nexus_discount_approval_policy_no_delete ON approvals_discountapprovalpolicy;
DROP FUNCTION IF EXISTS nexus_protect_discount_approval_policy();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("approvals", "0003_approval_revision_snapshots"),
    ]

    operations = [
        migrations.RunSQL(FORWARD_SQL, REVERSE_SQL),
    ]
