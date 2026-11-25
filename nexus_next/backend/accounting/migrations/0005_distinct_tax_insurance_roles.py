from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0004_tax_code_configuration"),
    ]

    operations = [
        migrations.AlterField(
            model_name="accountrole",
            name="role",
            field=models.CharField(
                max_length=32,
                choices=[
                    ("cash", "النقدية"),
                    ("bank", "البنوك"),
                    ("accounts_receivable", "العملاء"),
                    ("accounts_payable", "الموردون"),
                    ("inventory", "المخزون"),
                    ("sales", "المبيعات"),
                    ("sales_returns", "مردودات المبيعات"),
                    ("cogs", "تكلفة المبيعات"),
                    ("operating_expense", "المصروفات التشغيلية"),
                    ("vat_input", "ضريبة القيمة المضافة - مدخلات"),
                    ("vat_output", "ضريبة القيمة المضافة - مخرجات"),
                    ("vat_payable", "ضريبة قيمة مضافة مستحقة للسداد"),
                    ("employee_insurance_payable", "حصة الموظف من التأمينات المستحقة"),
                    ("employer_insurance_payable", "حصة الشركة من التأمينات المستحقة"),
                    ("employer_insurance_expense", "مصروف حصة الشركة في التأمينات"),
                    ("retained_earnings", "الأرباح المحتجزة"),
                    ("payroll_expense", "مصروف المرتبات"),
                    ("payroll_payable", "مرتبات مستحقة"),
                    ("goods_received_not_invoiced", "بضاعة مستلمة غير مفوترة"),
                    ("inventory_adjustment_gain", "أرباح تسوية المخزون"),
                    ("inventory_adjustment_loss", "خسائر تسوية المخزون"),
                    ("inter_branch_clearing", "حساب تسوية بين الفروع"),
                ],
            ),
        ),
    ]
