import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0004_stock_locations_expiry"),
        ("accounting", "0003_inventory_account_roles"),
    ]

    operations = [
        migrations.CreateModel(
            name="StockMovementJournalLink",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("journal_entry", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="stock_movement_links", to="accounting.journalentry")),
                ("movement", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="journal_links", to="inventory.stockmovement")),
            ],
            options={"ordering": ("id",), "constraints": [models.UniqueConstraint(fields=("movement", "journal_entry"), name="uq_stock_movement_journal_link")]},
        ),
    ]
