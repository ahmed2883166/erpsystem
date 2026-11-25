"""Idempotent company defaults shared by database initialization."""
from db_sql import executemany_compat, execute_compat
from db_compat import transactional_migration


CURRENCY_SEED = (
    ("SAR", "ريال سعودي", "ر.س"),
    ("EGP", "جنيه مصري", "ج.م"),
    ("USD", "دولار أمريكي", "$"),
    ("EUR", "يورو", "€"),
    ("AED", "درهم إماراتي", "د.إ"),
    ("GBP", "جنيه إسترليني", "£"),
    ("KWD", "دينار كويتي", "د.ك"),
    ("QAR", "ريال قطري", "ر.ق"),
)

COMPANY_DEFAULTS = {
    "company_name": "ADMRAIL ERP Demo",
    "tax_country": "EG",
    "tax_regime": "EG_VAT",
    "tax_number": "",
    "activity": "commercial",
    "base_currency": "EGP",
    "default_vat_rate": "14",
    "inventory_cost_method": "average",
}


@transactional_migration
def seed_company_defaults(connection):
    """Insert missing currency and company-setting defaults without overwrites."""
    executemany_compat(
        connection,
        "INSERT INTO currencies(code,name_ar,symbol,active,is_base,rate_to_base) "
        "VALUES(?,?,?,0,0,0) ON CONFLICT DO NOTHING",
        CURRENCY_SEED,
    )
    for key, value in COMPANY_DEFAULTS.items():
        execute_compat(
            connection,
            "INSERT INTO company_settings(setting_key,setting_value) "
            "VALUES(?,?) ON CONFLICT DO NOTHING",
            (key, value),
        )
    execute_compat(
        connection,
        "INSERT INTO currencies(code,name_ar,symbol,active,is_base,rate_to_base) "
        "VALUES('EGP','جنيه مصري','ج.م',1,1,1) ON CONFLICT DO NOTHING",
    )
    return True
