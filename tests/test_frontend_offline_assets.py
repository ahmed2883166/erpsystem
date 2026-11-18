from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class OfflineFrontendAssetTests(unittest.TestCase):
    def test_base_template_uses_only_local_bootstrap_assets(self):
        template = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
        self.assertNotIn("cdn.jsdelivr.net/npm/bootstrap@", template)
        self.assertNotIn("cdn.jsdelivr.net/npm/bootstrap-icons@", template)
        self.assertIn("vendor/bootstrap-5.3.3.rtl.min.css", template)
        self.assertIn("vendor/bootstrap-5.3.3.bundle.min.js", template)
        self.assertIn("bootstrap-icons-offline.css", template)

    def test_local_bootstrap_assets_are_present_and_versioned(self):
        css = ROOT / "static" / "vendor" / "bootstrap-5.3.3.rtl.min.css"
        js = ROOT / "static" / "vendor" / "bootstrap-5.3.3.bundle.min.js"
        icons = ROOT / "static" / "bootstrap-icons-offline.css"
        for path in (css, js, icons):
            self.assertTrue(path.is_file(), f"Missing local frontend asset: {path}")
            self.assertGreater(path.stat().st_size, 100)


if __name__ == "__main__":
    unittest.main()
