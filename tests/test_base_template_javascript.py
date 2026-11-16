"""Regression checks for JavaScript embedded in the shared base template."""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE_TEMPLATE = ROOT / "templates" / "base.html"


class BaseTemplateJavaScriptTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for JavaScript syntax validation")
    def test_inline_scripts_parse_without_syntax_errors(self):
        html = BASE_TEMPLATE.read_text(encoding="utf-8")
        scripts = re.findall(r"<script\b[^>]*>(.*?)</script\s*>", html, re.IGNORECASE | re.DOTALL)
        self.assertTrue(scripts, "Expected at least one inline script in base.html")

        for index, script in enumerate(scripts, start=1):
            with self.subTest(script=index):
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".js", encoding="utf-8", delete=False
                ) as source:
                    source.write(script)
                    script_path = source.name
                try:
                    result = subprocess.run(
                        [shutil.which("node"), "--check", script_path],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(
                        result.returncode,
                        0,
                        msg=result.stderr or result.stdout or f"Inline script {index} failed to parse",
                    )
                finally:
                    Path(script_path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
