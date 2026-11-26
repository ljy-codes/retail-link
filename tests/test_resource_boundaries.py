"""两平台运行资源边界；使用临时目录，不操作真实安装或用户数据。"""
import argparse
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server as host


class ResourceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="retail-resource-")
        self.root = Path(self.temp.name)
        self.install = self.root / "中文 空格 安装"
        self.app = self.install / "app"
        self.app.mkdir(parents=True)
        (self.install / "零售通.exe").write_bytes(b"isolated executable marker")
        (self.install / "runtime").mkdir()
        self.data = self.root / "user-data"
        self.originals = self.root / "originals"
        self.exports = self.root / "exports"
        self.patches = [
            mock.patch.object(host, "APP_DIR", self.app),
            mock.patch.object(host, "DESKTOP_MODE", True),
            mock.patch.object(host, "DATA_DIR", self.data),
            mock.patch.object(host, "ORIGINAL_DIR", self.originals),
            mock.patch.object(host, "EXPORT_DIR", self.exports),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.temp.cleanup()

    def test_windows_protects_entire_install_directory(self):
        self.assertEqual(host.resource_directory(), self.install.resolve())
        for directory in (self.install, self.install / "runtime", self.app):
            with self.subTest(directory=directory):
                with self.assertRaises(ValueError):
                    host.validate_export_target(directory / "用户结果.xlsx")
        self.assertEqual(
            host.validate_export_target(self.exports / "用户结果.xlsx"),
            (self.exports / "用户结果.xlsx").resolve(),
        )

    def test_runtime_refuses_install_root_data_before_creating_it(self):
        nested = self.install / "误选应用数据"
        args = argparse.Namespace(
            desktop=True, data_dir=nested, original_dir=self.originals,
            export_dir=self.exports, ready_file=None,
        )
        with self.assertRaises(ValueError):
            host.configure_runtime(args)
        self.assertFalse(nested.exists())

    def test_mac_bundle_and_browser_export_contracts_are_preserved(self):
        bundle = self.root / "零售通.app"
        with mock.patch.object(host, "APP_DIR", bundle / "Contents" / "Resources" / "app"):
            self.assertEqual(host.resource_directory(), bundle.resolve())
            with self.assertRaises(ValueError):
                host.validate_export_target(bundle / "result.xlsx")
        with mock.patch.object(host, "DESKTOP_MODE", False), \
                mock.patch.object(host, "EXPORT_DIR", self.app / "生成文件"):
            target = self.app / "生成文件" / "result.xlsx"
            self.assertEqual(host.validate_export_target(target), target.resolve())


if __name__ == "__main__":
    unittest.main()
