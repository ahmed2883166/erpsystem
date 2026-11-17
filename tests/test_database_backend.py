import unittest

from database_backend import configured_database_backend, require_supported_runtime_backend


class DatabaseBackendSelectionTests(unittest.TestCase):
    def test_defaults_to_existing_sqlite_runtime(self):
        self.assertEqual(configured_database_backend({}), "sqlite")
        self.assertEqual(require_supported_runtime_backend({}), "sqlite")

    def test_accepts_explicit_sqlite_selection(self):
        self.assertEqual(configured_database_backend({"NEXUS_DB_BACKEND": "SQLite"}), "sqlite")

    def test_normalizes_postgres_aliases(self):
        self.assertEqual(configured_database_backend({"NEXUS_DB_BACKEND": "postgres"}), "postgresql")
        self.assertEqual(configured_database_backend({"NEXUS_DB_BACKEND": "postgresql"}), "postgresql")

    def test_rejects_unknown_backend(self):
        with self.assertRaisesRegex(ValueError, "NEXUS_DB_BACKEND"):
            configured_database_backend({"NEXUS_DB_BACKEND": "mysql"})

    def test_postgresql_request_fails_closed_instead_of_using_sqlite(self):
        with self.assertRaisesRegex(RuntimeError, "PostgreSQL runtime is not enabled yet"):
            require_supported_runtime_backend({"NEXUS_DB_BACKEND": "postgresql"})


if __name__ == "__main__":
    unittest.main()
