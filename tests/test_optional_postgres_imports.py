"""Regression tests for optional PostgreSQL dependencies on SQLite startup."""
import subprocess
import sys
import unittest


class OptionalPostgreSQLImportTests(unittest.TestCase):
    def test_backup_modules_import_without_psycopg_installed(self):
        script = r"""
import builtins
original_import = builtins.__import__
def import_without_psycopg(name, *args, **kwargs):
    if name == "psycopg" or name.startswith("psycopg."):
        raise ModuleNotFoundError("simulated missing optional psycopg dependency")
    return original_import(name, *args, **kwargs)
builtins.__import__ = import_without_psycopg
import postgres_backup
import postgres_backup_scheduler
print("OPTIONAL_POSTGRES_IMPORTS_PASS")
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OPTIONAL_POSTGRES_IMPORTS_PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
