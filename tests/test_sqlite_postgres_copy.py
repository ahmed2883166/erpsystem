import unittest
from decimal import Decimal

from scripts.copy_sqlite_to_postgres import copy_database, dependency_order, normalize_value, order_self_referencing_rows, quote_ident


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

    def test_self_referencing_rows_are_parent_first(self):
        rows = [
            {"id": 3, "parent_id": 2},
            {"id": 1, "parent_id": None},
            {"id": 2, "parent_id": 1},
        ]
        ordered = order_self_referencing_rows(
            rows,
            [{"id": 0, "seq": 0, "from": "parent_id", "to": "id"}],
            "accounts",
        )
        self.assertEqual([row["id"] for row in ordered], [1, 2, 3])

    def test_self_reference_cycles_are_rejected(self):
        rows = [{"id": 1, "parent_id": 2}, {"id": 2, "parent_id": 1}]
        with self.assertRaisesRegex(ValueError, "cycle"):
            order_self_referencing_rows(
                rows,
                [{"id": 0, "seq": 0, "from": "parent_id", "to": "id"}],
                "accounts",
            )

    def test_copy_requires_explicit_apply_before_any_connection(self):
        with self.assertRaisesRegex(ValueError, "disabled by default"):
            copy_database("missing.sqlite", "postgresql://invalid", apply=False)

    def test_copy_refuses_public_schema_before_connecting(self):
        with self.assertRaisesRegex(ValueError, "dedicated, isolated"):
            copy_database("missing.sqlite", "postgresql://invalid", schema="public", apply=True)

    def test_identifiers_are_quoted(self):
        self.assertEqual(quote_ident('odd"name'), '"odd""name"')


if __name__ == "__main__":
    unittest.main()
