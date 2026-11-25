from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("approvals", "0009_optional_requested_discount_limit"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="discountapprovalpolicy",
            options={
                "verbose_name": "سياسة حدود الموافقات",
                "verbose_name_plural": "سياسات حدود الموافقات",
            },
        ),
    ]
