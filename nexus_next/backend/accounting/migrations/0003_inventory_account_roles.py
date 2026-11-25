from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounting", "0002_initial")]

    operations = [
        migrations.AlterField(
            model_name="accountrole",
            name="role",
            field=models.CharField(choices=[
                ("cash", "النقدية"), ("bank", "البنوك"),
                ("accounts_receivable", "العملاء"), ("accounts_payable", "الموردون"),
                ("inventory", "المخزون"), ("sales", "المبيعات"),
                ("sales_returns", "مردودات المبيعات"), ("cogs", "تكلفة المبيعات"),
                ("operating_expense", "المصروفات التشغيلية"),
                ("vat_input", "ضريبة القيمة المضافة - مدخلات"),
                ("vat_output", "ضريبة القيمة المضافة - مخرجات"),
                ("retained_earnings", "الأرباح المحتجزة"),
                ("payroll_expense", "مصروف المرتبات"), ("payroll_payable", "مرتبات مستحقة"),
                ("goods_received_not_invoiced", "بضاعة مستلمة غير مفوترة"),
                ("inventory_adjustment_gain", "أرباح تسوية المخزون"),
                ("inventory_adjustment_loss", "خسائر تسوية المخزون"),
                ("inter_branch_clearing", "حساب تسوية بين الفروع"),
            ], max_length=32),
        ),
    ]
