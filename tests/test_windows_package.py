"""Windows desktop source, bridge and release-package contracts."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / "desktop" / "windows"
sys.path.insert(0, str(ROOT))


def load_build_module():
    spec = importlib.util.spec_from_file_location("retail_windows_build", WINDOWS / "build_windows.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WindowsPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = load_build_module()
        cls.release = json.loads((ROOT / "desktop" / "release.json").read_text(encoding="utf-8"))

    def test_release_pair_uses_mac_odd_and_windows_even_versions(self):
        release = self.build.validate_release(self.release)
        self.assertEqual(release["pairedMacVersion"], "0.1.9")
        self.assertEqual(release["windowsVersion"], "0.1.10")
        self.assertEqual(release["featureLevel"], "desktop-3")

    def test_windows_metadata_reuses_the_shared_feature_contract(self):
        metadata = self.build.windows_metadata(self.release)
        self.assertEqual(metadata, {
            "name": "零售通",
            "version": "0.1.10",
            "platform": "windows",
            "featureLevel": "desktop-3",
            "pairedMacVersion": "0.1.9",
        })

    def test_source_layout_separates_app_installer_and_uninstaller(self):
        expected = [
            "app/RetailLink.Windows.csproj",
            "app/Program.cs",
            "app/MainForm.cs",
            "app/BackendManager.cs",
            "app/NativeBridge.cs",
            "installer/RetailLink.Setup.csproj",
            "installer/Program.cs",
            "uninstall/uninstall.ps1",
            "build_windows.py",
            "README.md",
            "RetailLink.ico",
        ]
        self.assertEqual([name for name in expected if not (WINDOWS / name).is_file()], [])

    def test_native_bridge_uses_the_existing_web_adapter_contract(self):
        script = (ROOT / "web" / "desktop.js").read_text(encoding="utf-8")
        bridge = (WINDOWS / "app" / "NativeBridge.cs").read_text(encoding="utf-8")
        main = (WINDOWS / "app" / "MainForm.cs").read_text(encoding="utf-8")
        combined = bridge + main
        for contract in ("retailLinkDesktop", "postMessage", "RetailLinkDesktop.resolve",
                         "chooseDirectory", "chooseFile", "saveFile", "showInFolder"):
            self.assertIn(contract, script)
            self.assertIn(contract, combined)

    def test_payload_guard_rejects_user_data_build_cache_and_installer_sources(self):
        forbidden = [
            Path("app/本地数据/history.json"),
            Path("app/原始资料/example.xlsx"),
            Path("app/生成文件/export.xlsx"),
            Path("app/__pycache__/server.cpython-312.pyc"),
            Path("app/desktop/windows/app/MainForm.cs"),
            Path("app/测试空间/run.log"),
            Path("app/导出结果.xlsx"),
            Path("app/operation.sqlite3"),
            Path("app/操作日志.jsonl"),
        ]
        for relative in forbidden:
            self.assertTrue(self.build.forbidden_payload_path(relative), relative.as_posix())
        allowed = [Path("零售通.exe"), Path("runtime/python.exe"), Path("app/server.py"),
                   Path("app/backend/analyzer.py"), Path("app/web/index.html"),
                   Path("app/版本.json"), Path("app/功能清单.json")]
        for relative in allowed:
            self.assertFalse(self.build.forbidden_payload_path(relative), relative.as_posix())

    def test_installer_and_uninstaller_use_per_user_locations_and_preserve_documents(self):
        installer = (WINDOWS / "installer" / "Program.cs").read_text(encoding="utf-8")
        uninstaller = (WINDOWS / "uninstall" / "uninstall.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("LOCALAPPDATA", installer)
        self.assertIn("零售通", installer)
        self.assertIn("UninstallString", installer)
        self.assertIn("RetailLink.Windows", installer)
        self.assertIn("LOCALAPPDATA", uninstaller)
        self.assertIn("Documents", uninstaller)
        self.assertIn("普通卸载", uninstaller)
        self.assertIn("彻底卸载", uninstaller)
        self.assertNotIn("Remove-Item -LiteralPath $documents -Recurse", uninstaller)

    def test_powershell_script_is_packaged_with_utf8_bom(self):
        source = WINDOWS / "uninstall" / "uninstall.ps1"
        plain = source.read_bytes().lstrip(b"\xef\xbb\xbf")
        packaged = self.build.ensure_powershell_utf8_bom(plain)
        self.assertTrue(packaged.startswith(b"\xef\xbb\xbf"))
        self.assertIn("普通卸载".encode("utf-8"), packaged)

    def test_uninstaller_parses_in_windows_powershell_5(self):
        powershell = shutil.which("powershell.exe")
        if not powershell:
            self.skipTest("Windows PowerShell 5.1 不可用")
        packaged = ROOT / "tests" / ".windows-uninstall-parser-test.ps1"
        try:
            packaged.write_bytes(
                self.build.powershell_script_bytes(WINDOWS / "uninstall" / "uninstall.ps1")
            )
            escaped = str(packaged).replace("'", "''")
            command = (
                "$tokens=$null; $errors=$null; "
                f"[void][System.Management.Automation.Language.Parser]::ParseFile('{escaped}',"
                "[ref]$tokens,[ref]$errors); "
                "if($errors.Count){$errors|ForEach-Object{Write-Output $_.Message}; exit 1}"
            )
            result = subprocess.run(
                [powershell, "-NoProfile", "-Command", command],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        finally:
            packaged.unlink(missing_ok=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
