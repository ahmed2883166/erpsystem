"""Run the NEXUS test suite against an isolated temporary data directory."""
import os
import sys
import tempfile
import unittest


def main():
    with tempfile.TemporaryDirectory(prefix="nexus-erp-tests-") as data_dir:
        os.environ["NEXUS_DATA_DIR"] = data_dir
        os.environ["NEXUS_BACKUP_DIR"] = os.path.join(data_dir, "backups")
        # Unit/integration tests use the explicit local demo mode; production defaults fail closed.
        os.environ["NEXUS_DEMO_MODE"] = "1"
        suite = unittest.defaultTestLoader.discover("tests")
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1
    finally:
        # Windows locks open rotating log files and prevents TemporaryDirectory cleanup.
        application = sys.modules.get("app")
        if application is not None:
            for handler in tuple(application.app.logger.handlers):
                if getattr(handler, "_nexus_http_500_handler", False):
                    application.app.logger.removeHandler(handler)
                    handler.close()


if __name__ == "__main__":
    sys.exit(main())
