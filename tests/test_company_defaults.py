import sqlite3
import unittest

from company_defaults import CURRENCY_SEED, COMPANY_DEFAULTS, seed_company_defaults


class CompanyDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute(
            "CREATE TABLE currencies (code TEXT PRIMARY KEY, name_ar TEXT NOT NULL, "
            "symbol TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 0, "
            "is_base INTEGER NOT NULL DEFAULT 0, rate_to_base REAL NOT NULL DEFAULT 0)"
        )
        self.connection.execute(
            "CREATE TABLE company_settings (setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL)"
        )

    def tearDown(self):
        self.connection.close()

    def test_seeds_currencies_and_company_defaults(self):
        self.assertTrue(seed_company_defaults(self.connection))
        currencies = {
            row[0] for row in self.connection.execute("SELECT code FROM currencies")
        }
        settings = dict(self.connection.execute(
            "SELECT setting_key,setting_value FROM company_settings"
        ))
        self.assertTrue({row[0] for row in CURRENCY_SEED}.issubset(currencies))
        self.assertEqual(settings, COMPANY_DEFAULTS)

    def test_repeated_seed_preserves_company_overrides_and_currency_rows(self):
        seed_company_defaults(self.connection)
        self.connection.execute(
            "UPDATE company_settings SET setting_value='شركة العميل' "
            "WHERE setting_key='company_name'"
        )
        self.connection.execute(
            "UPDATE currencies SET rate_to_base=49.5 WHERE code='EGP'"
        )
        self.connection.commit()

        self.assertTrue(seed_company_defaults(self.connection))
        settings = dict(self.connection.execute(
            "SELECT setting_key,setting_value FROM company_settings"
        ))
        egp = self.connection.execute(
            "SELECT rate_to_base FROM currencies WHERE code='EGP'"
        ).fetchone()[0]
        self.assertEqual(settings["company_name"], "شركة العميل")
        self.assertEqual(egp, 49.5)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM currencies").fetchone()[0],
            len(CURRENCY_SEED),
        )


if __name__ == "__main__":
    unittest.main()
