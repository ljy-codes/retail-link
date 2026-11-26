"""Static contract for the macOS A02 icon source and build wiring."""
from pathlib import Path
import json
import unittest


ROOT = Path(__file__).resolve().parents[1]


class MacOSIconSourceTests(unittest.TestCase):
    def test_a02_source_is_the_mac_icon_input(self):
        source = ROOT / "docs/设计/图标概念/零售通-A02-通路.svg"
        self.assertTrue(source.is_file())
        svg = source.read_text(encoding="utf-8")
        self.assertIn("#0B4F4A", svg)
        self.assertIn("#DEFFF2", svg)
        builder = (ROOT / "desktop/build_macos.py").read_text(encoding="utf-8")
        self.assertIn("零售通-A02-通路.svg", builder)
        self.assertIn('"sips"', builder)

    def test_release_features_name_a02_icon(self):
        release = json.loads((ROOT / "desktop/release.json").read_text(encoding="utf-8"))
        self.assertIn("a02_channel_icon", release["features"])
        self.assertNotIn("red_chart_icon", release["features"])

    def test_bundle_declares_the_macos_icon(self):
        builder = (ROOT / "desktop/build_macos.py").read_text(encoding="utf-8")
        self.assertIn('info["CFBundleIconFile"] = "RetailLink.icns"', builder)
        self.assertIn('"RetailLink.icns"', builder)


if __name__ == "__main__":
    unittest.main(verbosity=2)
