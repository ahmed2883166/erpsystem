import os
import subprocess
import sys
import tempfile
import unittest

from app import app


class PublicContactAndDeploymentSecurityTests(unittest.TestCase):
    def test_about_page_shows_only_the_supplied_public_contact_details(self):
        response = app.test_client().get("/about")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("ahmed28831666@gmail.com", page)
        self.assertIn("mailto:ahmed28831666@gmail.com", page)
        self.assertIn("01002296868", page)
        self.assertIn("https://wa.me/201002296868", page)
        self.assertIn("طلب نسخة تجريبية", page)

    def test_demo_bypass_is_disabled_when_environment_is_unconfigured(self):
        with tempfile.TemporaryDirectory(prefix="nexus-production-config-") as data_dir:
            env = os.environ.copy()
            env.pop("NEXUS_DEMO_MODE", None)
            env["NEXUS_SKIP_DB_INIT"] = "1"
            env["NEXUS_DATA_DIR"] = data_dir
            env["NEXUS_BACKUP_DIR"] = os.path.join(data_dir, "backups")
            result = subprocess.run(
                [sys.executable, "-c", "import app; assert app.NEXUS_DEMO_MODE is False"],
                env=env,
                capture_output=True,
                text=True,
                timeout=45,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
