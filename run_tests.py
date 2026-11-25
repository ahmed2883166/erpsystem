"""Run the NEXUS test suite against an isolated temporary data directory."""
import os
import sys
import tempfile
import unittest


def _close_test_log_handlers():
    # Windows locks open rotating log files and prevents TemporaryDirectory cleanup.
    application = sys.modules.get("app")
    if application is None:
        return
    for handler in tuple(application.app.logger.handlers):
        if getattr(handler, "_nexus_http_500_handler", False):
            application.app.logger.removeHandler(handler)
            handler.close()


def main():
    with tempfile.TemporaryDirectory(prefix="nexus-erp-tests-") as data_dir:
        os.environ["NEXUS_DATA_DIR"] = data_dir
        os.environ["NEXUS_BACKUP_DIR"] = os.path.join(data_dir, "backups")
        # Unit/integration tests use the explicit local demo mode; production defaults fail closed.
        os.environ["NEXUS_DEMO_MODE"] = "1"
        try:
            suite = unittest.defaultTestLoader.discover("tests")
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            return 0 if result.wasSuccessful() else 1
        finally:
            # Close the handler before the temporary directory context removes its files.
            _close_test_log_handlers()


if __name__ == "__main__":
    sys.exit(main())
