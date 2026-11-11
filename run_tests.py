"""Run the NEXUS test suite against an isolated temporary data directory."""
import os
import sys
import tempfile
import unittest


def main():
    with tempfile.TemporaryDirectory(prefix="nexus-erp-tests-") as data_dir:
        os.environ["NEXUS_DATA_DIR"] = data_dir
        os.environ["NEXUS_BACKUP_DIR"] = os.path.join(data_dir, "backups")
        suite = unittest.defaultTestLoader.discover("tests")
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
