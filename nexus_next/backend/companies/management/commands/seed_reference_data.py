from django.core.management.base import BaseCommand
from companies.models import Currency

class Command(BaseCommand):
    help = "Create the standard NEXUS currency reference records without changing existing records."

    def handle(self, *args, **options):
        currencies = [
            ("EGP", "جنيه مصري", "Egyptian Pound", "ج.م", 2),
            ("SAR", "ريال سعودي", "Saudi Riyal", "ر.س", 2),
            ("USD", "دولار أمريكي", "US Dollar", "$", 2),
            ("EUR", "يورو", "Euro", "€", 2),
        ]
        created = 0
        for code, name_ar, name_en, symbol, decimals in currencies:
            _, was_created = Currency.objects.get_or_create(
                code=code,
                defaults={
                    "name_ar": name_ar,
                    "name_en": name_en,
                    "symbol": symbol,
                    "decimal_places": decimals,
                },
            )
            created += int(was_created)
        self.stdout.write(self.style.SUCCESS(
            f"Currency reference data ready. Newly created: {created}; existing records preserved."
        ))
