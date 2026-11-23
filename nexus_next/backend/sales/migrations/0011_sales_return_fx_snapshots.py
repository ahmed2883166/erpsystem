from decimal import Decimal

from django.db import migrations, models


def backfill_sales_return_fx(apps, schema_editor):
    SalesReturnLine = apps.get_model("sales", "SalesReturnLine")
    SalesReturn = apps.get_model("sales", "SalesReturn")
    for line in SalesReturnLine.objects.select_related(
        "sales_return__invoice", "invoice_line__delivery_line"
    ).iterator():
        invoice = line.sales_return.invoice
        if not invoice.transaction_currency_id:
            continue
        original_quantity = line.invoice_line.delivery_line.quantity
        if (
            not original_quantity
            or line.invoice_line.transaction_net_amount is None
            or line.invoice_line.transaction_tax_amount is None
        ):
            continue
        ratio = line.quantity / original_quantity
        transaction_net = (
            line.invoice_line.transaction_net_amount * ratio
        ).quantize(Decimal("0.0001"))
        transaction_tax = (
            line.invoice_line.transaction_tax_amount * ratio
        ).quantize(Decimal("0.0001"))
        SalesReturnLine.objects.filter(pk=line.pk).update(
            transaction_net_amount=transaction_net,
            transaction_tax_amount=transaction_tax,
            transaction_total_amount=transaction_net + transaction_tax,
        )

    for sales_return in SalesReturn.objects.filter(
        invoice__transaction_currency__isnull=False
    ).iterator():
        if sales_return.lines.filter(
            transaction_net_amount__isnull=True
        ).exists() or sales_return.lines.filter(
            transaction_tax_amount__isnull=True
        ).exists():
            continue
        totals = sales_return.lines.aggregate(
            net=models.Sum("transaction_net_amount"),
            tax=models.Sum("transaction_tax_amount"),
        )
        transaction_net = totals["net"] or Decimal("0")
        transaction_tax = totals["tax"] or Decimal("0")
        SalesReturn.objects.filter(pk=sales_return.pk).update(
            transaction_net_total=transaction_net,
            transaction_tax_total=transaction_tax,
            transaction_grand_total=transaction_net + transaction_tax,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("sales", "0010_sales_return_reversal"),
    ]

    operations = [
        migrations.AddField(
            model_name="salesreturn",
            name="transaction_net_total",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True),
        ),
        migrations.AddField(
            model_name="salesreturn",
            name="transaction_tax_total",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True),
        ),
        migrations.AddField(
            model_name="salesreturn",
            name="transaction_grand_total",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True),
        ),
        migrations.AddField(
            model_name="salesreturnline",
            name="transaction_net_amount",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True),
        ),
        migrations.AddField(
            model_name="salesreturnline",
            name="transaction_tax_amount",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True),
        ),
        migrations.AddField(
            model_name="salesreturnline",
            name="transaction_total_amount",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True),
        ),
        migrations.RunPython(backfill_sales_return_fx, migrations.RunPython.noop),
    ]
