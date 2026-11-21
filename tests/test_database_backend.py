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

    def test_native_profile_defaults_to_postgresql(self):
        env = {
            "NEXUS_DEPLOYMENT_PROFILE": "postgresql_native",
            "NEXUS_POSTGRES_DSN": "postgresql://test-only",
            "NEXUS_POSTGRES_RUNTIME_ENABLE": "1",
        }
        self.assertEqual(configured_database_backend(env), "postgresql")
        self.assertEqual(require_supported_runtime_backend(env), "postgresql")

    def test_native_profile_rejects_sqlite_override(self):
        with self.assertRaisesRegex(ValueError, "requires NEXUS_DB_BACKEND=postgresql"):
            configured_database_backend({
                "NEXUS_DEPLOYMENT_PROFILE": "postgresql_native",
                "NEXUS_DB_BACKEND": "sqlite",
            })

    def test_rejects_unknown_backend(self):
        with self.assertRaisesRegex(ValueError, "NEXUS_DB_BACKEND"):
            configured_database_backend({"NEXUS_DB_BACKEND": "mysql"})

    def test_postgresql_runtime_requires_explicit_enable_flag(self):
        with self.assertRaisesRegex(RuntimeError, "NEXUS_POSTGRES_RUNTIME_ENABLE=1"):
            require_supported_runtime_backend({
                "NEXUS_DB_BACKEND": "postgresql",
                "NEXUS_POSTGRES_DSN": "postgresql://test-only",
            })

    def test_postgresql_runtime_accepts_explicit_enable_flag(self):
        self.assertEqual(require_supported_runtime_backend({
            "NEXUS_DB_BACKEND": "postgresql",
            "NEXUS_POSTGRES_DSN": "postgresql://test-only",
            "NEXUS_POSTGRES_RUNTIME_ENABLE": "1",
        }), "postgresql")

    def test_postgresql_runtime_rejects_missing_dsn_even_when_enabled(self):
        with self.assertRaisesRegex(RuntimeError, "DSN is required"):
            require_supported_runtime_backend({
                "NEXUS_DB_BACKEND": "postgresql",
                "NEXUS_POSTGRES_RUNTIME_ENABLE": "1",
            })


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
