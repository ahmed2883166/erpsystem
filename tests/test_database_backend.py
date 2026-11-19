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

    def test_postgresql_runtime_remains_blocked_until_acceptance_gates_pass(self):
        with self.assertRaisesRegex(RuntimeError, "temporarily disabled"):
            require_supported_runtime_backend({"NEXUS_DB_BACKEND": "postgresql"})


class DatabaseConnectionFactoryGateTests(unittest.TestCase):
    def test_postgresql_connection_factory_never_falls_back_to_sqlite(self):
        from unittest.mock import patch
        from database import connect_database

        with patch.dict("os.environ", {
            "NEXUS_DB_BACKEND": "postgresql",
            "NEXUS_POSTGRES_DSN": "",
        }):
            with self.assertRaisesRegex(RuntimeError, "DSN is required"):
                connect_database(":memory:")


if __name__ == "__main__":
    unittest.main()
