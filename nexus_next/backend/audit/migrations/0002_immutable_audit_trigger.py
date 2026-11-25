from django.db import migrations


FORWARD_SQL = """
CREATE OR REPLACE FUNCTION nexus_reject_audit_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'NEXUS audit events are append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER nexus_audit_event_immutable
BEFORE UPDATE OR DELETE ON audit_auditevent
FOR EACH ROW EXECUTE FUNCTION nexus_reject_audit_mutation();
"""

REVERSE_SQL = """
DROP TRIGGER IF EXISTS nexus_audit_event_immutable ON audit_auditevent;
DROP FUNCTION IF EXISTS nexus_reject_audit_mutation();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(FORWARD_SQL, REVERSE_SQL),
    ]
