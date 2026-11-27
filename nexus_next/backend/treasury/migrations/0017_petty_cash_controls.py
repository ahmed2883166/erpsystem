# Generated for controlled petty cash workflows.
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("treasury", "0016_petty_cash"),
        ("accounting", "0001_initial"),
        ("companies", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="pettycashexpense",
            name="receipt_file",
            field=models.FileField(blank=True, null=True, upload_to="petty_cash_receipts/%Y/%m/"),
        ),
        migrations.AddField(
            model_name="pettycashexpense",
            name="submitted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="pettycashexpense",
            name="approved_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="pettycashexpense",
            name="approval_note",
            field=models.CharField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name="pettycashexpense",
            name="rejected_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="pettycashexpense",
            name="rejection_note",
            field=models.CharField(blank=True, max_length=500),
        ),
        migrations.AlterField(
            model_name="pettycashexpense",
            name="status",
            field=models.CharField(
                choices=[
                    ("draft", "مسودة"),
                    ("pending_approval", "بانتظار الاعتماد"),
                    ("approved", "معتمد"),
                    ("rejected", "مرفوض"),
                    ("settled", "تمت التسوية"),
                    ("void", "ملغاة"),
                ],
                default="draft",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="pettycashexpense",
            name="approved_by",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="approved_petty_cash_expenses", to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="pettycashexpense",
            name="rejected_by",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="rejected_petty_cash_expenses", to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="pettycashcount",
            name="status",
            field=models.CharField(
                choices=[("pending", "بانتظار الاعتماد"), ("approved", "معتمد"), ("rejected", "مرفوض")],
                default="pending", max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="pettycashcount",
            name="reviewed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="pettycashcount",
            name="review_note",
            field=models.CharField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name="pettycashcount",
            name="reviewed_by",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="reviewed_petty_cash_counts", to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="pettycashcount",
            name="adjustment_journal_entry",
            field=models.OneToOneField(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="petty_cash_count_adjustment", to="accounting.journalentry",
            ),
        ),
        migrations.CreateModel(
            name="PettyCashCustodianHandover",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("handover_date", models.DateField()),
                ("expected_balance", models.DecimalField(decimal_places=4, max_digits=20)),
                ("actual_balance", models.DecimalField(decimal_places=4, max_digits=20)),
                ("notes", models.CharField(blank=True, max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("fund", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="custodian_handovers", to="treasury.pettycashfund")),
                ("previous_custodian", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="petty_cash_handovers_from", to=settings.AUTH_USER_MODEL)),
                ("new_custodian", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="petty_cash_handovers_to", to=settings.AUTH_USER_MODEL)),
                ("handed_over_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="petty_cash_handovers_recorded", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("-handover_date", "-id")},
        ),
    ]
