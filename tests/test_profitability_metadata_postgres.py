"""PostgreSQL metadata compatibility tests for profitability report helpers.

All objects are created in a disposable schema and rolled back. No app database
or company data is read or modified.
"""
import os
import unittest
import uuid


@unittest.skipUnless(
    os.environ.get("NEXUS_TEST_POSTGRES_DSN"),
    "NEXUS_TEST_POSTGRES_DSN is not configured",
)
class ProfitabilityMetadataPostgreSQLTests(unittest.TestCase):
    def test_profitability_metadata_helpers_use_current_postgres_schema(self):
        import psycopg
        from psycopg.rows import dict_row
        from profitability_reports import _columns, _table_exists

        schema = "nexus_profit_meta_" + uuid.uuid4().hex[:12]
        connection = psycopg.connect(
            os.environ["NEXUS_TEST_POSTGRES_DSN"], row_factory=dict_row
        )
        try:
            try:
                with connection.transaction():
                    connection.execute(f'CREATE SCHEMA "{schema}"')
                    connection.execute(f'SET LOCAL search_path TO "{schema}"')
                    connection.execute(
                        "CREATE TABLE journal_lines "
                        "(id BIGINT PRIMARY KEY, branch_id BIGINT, debit NUMERIC(24,6))"
                    )
                    self.assertTrue(_table_exists(connection, "journal_lines"))
                    self.assertFalse(_table_exists(connection, "missing_table"))
                    self.assertEqual(
                        _columns(connection, "journal_lines"),
                        {"id", "branch_id", "debit"},
                    )
                    self.assertEqual(_columns(connection, "missing_table"), set())
                    raise RuntimeError("rollback disposable schema")
            except RuntimeError as exc:
                self.assertEqual(str(exc), "rollback disposable schema")
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
