import unittest
from decimal import Decimal

from scripts.copy_sqlite_to_postgres import copy_database, dependency_order, normalize_value, quote_ident


class SQLitePostgresCopyPlanningTests(unittest.TestCase):
    def test_dependency_order_places_parent_before_child(self):
        result = dependency_order(
            ["journal_lines", "accounts", "journal_entries"],
            {
                "journal_lines": {"journal_entries", "accounts"},
                "journal_entries": set(),
                "accounts": set(),
            },
        )
        self.assertLess(result.index("accounts"), result.index("journal_lines"))
        self.assertLess(result.index("journal_entries"), result.index("journal_lines"))

    def test_dependency_order_rejects_cycles(self):
        with self.assertRaisesRegex(ValueError, "Foreign-key cycle"):
            dependency_order(["a", "b"], {"a": {"b"}, "b": {"a"}})

    def test_float_values_are_normalized_without_binary_float(self):
        self.assertEqual(normalize_value(0.1), Decimal("0.1"))
        self.assertEqual(normalize_value("0.10"), "0.10")

    def test_copy_requires_explicit_apply_before_any_connection(self):
        with self.assertRaisesRegex(ValueError, "disabled by default"):
            copy_database("missing.sqlite", "postgresql://invalid", apply=False)

    def test_identifiers_are_quoted(self):
        self.assertEqual(quote_ident('odd"name'), '"odd""name"')


if __name__ == "__main__":
    unittest.main()
