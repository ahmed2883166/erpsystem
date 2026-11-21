from django.db import migrations, models
import django.core.validators


class Migration(migrations.Migration):
    dependencies = [
        ("approvals", "0010_approval_policy_options"),
        ("purchasing", "0001_initial"),
        ("treasury", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="discountapprovalpolicy",
            name="max_purchase_invoice_amount",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True,
                validators=[django.core.validators.MinValueValidator(0)],
                help_text="أقصى إجمالي فاتورة مشتريات دون موافقة مدير الشركة."),
        ),
        migrations.AddField(
            model_name="discountapprovalpolicy",
            name="max_payment_amount",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True,
                validators=[django.core.validators.MinValueValidator(0)],
                help_text="أقصى قيمة سند صرف دون موافقة مدير الشركة."),
        ),
        migrations.RemoveConstraint(
            model_name="discountapprovalpolicy",
            name="discount_policy_has_limit",
        ),
        migrations.AddConstraint(
            model_name="discountapprovalpolicy",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(max_discount_percent__isnull=False)
                    | models.Q(max_sales_invoice_amount__isnull=False)
                    | models.Q(max_purchase_invoice_amount__isnull=False)
                    | models.Q(max_payment_amount__isnull=False)
                ),
                name="discount_policy_has_limit",
            ),
        ),
        migrations.CreateModel(
            name="FinancialApprovalRequest",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("document_type", models.CharField(choices=[("purchase_invoice", "فاتورة مشتريات"), ("supplier_payment", "سند صرف مورد"), ("treasury_payment", "سند صرف خزينة")], max_length=24)),
                ("requested_limit", models.DecimalField(decimal_places=4, max_digits=20)),
                ("requested_amount", models.DecimalField(decimal_places=4, max_digits=20)),
                ("requested_snapshot", models.JSONField(default=dict, editable=False)),
                ("status", models.CharField(choices=[("pending", "بانتظار الموافقة"), ("approved", "تمت الموافقة"), ("rejected", "مرفوض"), ("superseded", "تم استبداله بطلب أحدث")], default="pending", max_length=10)),
                ("decision_note", models.CharField(blank=True, max_length=500)),
                ("requested_at", models.DateTimeField(auto_now_add=True)),
                ("decided_at", models.DateTimeField(blank=True, null=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="financial_approval_requests", to="companies.company")),
                ("decided_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="decided_financial_approval_requests", to="accounts.user")),
                ("purchase_invoice", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="approval_requests", to="purchasing.purchaseinvoice")),
                ("requested_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="created_financial_approval_requests", to="accounts.user")),
                ("supplier_payment", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="approval_requests", to="purchasing.supplierpayment")),
                ("treasury_transaction", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="approval_requests", to="treasury.treasurytransaction")),
            ],
            options={
                "ordering": ("status", "-requested_at"),
                "indexes": [models.Index(fields=["company", "status", "requested_at"], name="fin_approval_co_status_idx")],
                "constraints": [
                    models.CheckConstraint(
                        condition=(
                            models.Q(document_type="purchase_invoice", purchase_invoice__isnull=False, supplier_payment__isnull=True, treasury_transaction__isnull=True)
                            | models.Q(document_type="supplier_payment", purchase_invoice__isnull=True, supplier_payment__isnull=False, treasury_transaction__isnull=True)
                            | models.Q(document_type="treasury_payment", purchase_invoice__isnull=True, supplier_payment__isnull=True, treasury_transaction__isnull=False)
                        ),
                        name="financial_approval_one_document",
                    ),
                ],
            },
        ),
    ]
