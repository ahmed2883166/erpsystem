from django.db import migrations


class Migration(migrations.Migration):
    """Keep reference-currency seeding out of test database creation.

    The deployment/bootstrap command seeds EGP, SAR, USD and EUR idempotently.
    Tests create their own currency fixtures and must start from an empty table.
    """

    dependencies = [("companies", "0006_ensure_company_head_office")]

    operations = []
