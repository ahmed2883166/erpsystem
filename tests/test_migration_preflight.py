import unittest

from scripts.audit_sqlite_postgres_migration import build_report


class SQLitePostgresMigrationPreflightTests(unittest.TestCase):
    def test_preflight_passes_only_for_complete_empty_target(self):
        source = {
            "integrity_check": ["ok"],
            "foreign_key_violations": [],
            "tables": {
                "accounts": {"columns": ["id", "code"], "row_count": 3},
                "journal_entries": {"columns": ["id"], "row_count": 2},
            },
        }
        target = {
            "tables": {
                "accounts": {"columns": ["id", "code"], "row_count": 0},
                "journal_entries": {"columns": ["id"], "row_count": 0},
            }
        }
        report = build_report(source, target)
        self.assertTrue(report["preflight_passed"])
        self.assertEqual(report["source_row_count"], 5)

    def test_preflight_blocks_missing_schema_and_nonempty_target(self):
        source = {
            "integrity_check": ["ok"],
            "foreign_key_violations": [],
            "tables": {
                "accounts": {"columns": ["id", "code", "active"], "row_count": 3},
                "journal_entries": {"columns": ["id"], "row_count": 2},
            },
        }
        target = {
            "tables": {
                "accounts": {"columns": ["id", "code"], "row_count": 1},
            }
        }
        report = build_report(source, target)
        self.assertFalse(report["preflight_passed"])
        self.assertEqual(report["missing_postgres_tables"], ["journal_entries"])
        self.assertEqual(report["column_differences"][0]["missing_in_postgres"], ["active"])
        self.assertEqual(report["nonempty_postgres_tables"][0]["table"], "accounts")

    def test_preflight_blocks_source_integrity_and_foreign_key_issues(self):
        source = {
            "integrity_check": ["database disk image is malformed"],
            "foreign_key_violations": [{"table": "journal_lines", "rowid": 7}],
            "tables": {"journal_lines": {"columns": ["id"], "row_count": 1}},
        }
        target = {
            "tables": {"journal_lines": {"columns": ["id"], "row_count": 0}}
        }
        report = build_report(source, target)
        self.assertFalse(report["preflight_passed"])
        self.assertFalse(report["source_integrity_ok"])
        self.assertFalse(report["source_foreign_keys_ok"])


if __name__ == "__main__":
    unittest.main()
