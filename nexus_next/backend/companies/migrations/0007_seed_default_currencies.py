from django.db import migrations


def ensure_default_currencies(apps, schema_editor):
    """Install the standard selectable currencies without overwriting existing setup."""
    Currency = apps.get_model("companies", "Currency")
    database = schema_editor.connection.alias
    currencies = (
        ("EGP", "جنيه مصري", "Egyptian Pound", "ج.م", 2),
        ("SAR", "ريال سعودي", "Saudi Riyal", "ر.س", 2),
        ("USD", "دولار أمريكي", "US Dollar", "$", 2),
        ("EUR", "يورو", "Euro", "€", 2),
    )
    for code, name_ar, name_en, symbol, decimal_places in currencies:
        Currency.objects.using(database).get_or_create(
            code=code,
            defaults={
                "name_ar": name_ar,
                "name_en": name_en,
                "symbol": symbol,
                "decimal_places": decimal_places,
                "is_active": True,
            },
        )


class Migration(migrations.Migration):
    dependencies = [("companies", "0006_ensure_company_head_office")]

    operations = [
        migrations.RunPython(ensure_default_currencies, migrations.RunPython.noop),
    ]
