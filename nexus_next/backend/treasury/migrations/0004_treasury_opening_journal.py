from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0006_payroll_tax_payable_role"),
        ("treasury", "0003_cash_flow_category"),
    ]

    operations = [
        migrations.AddField(
            model_name="treasuryaccount",
            name="opening_journal_entry",
            field=models.OneToOneField(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="treasury_account_opening", to="accounting.journalentry",
            ),
        ),
    ]
