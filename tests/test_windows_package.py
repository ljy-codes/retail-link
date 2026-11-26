"""Windows desktop source, bridge and release-package contracts."""
from __future__ import annotations

import html
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib

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
        cls.legacy_release = json.loads((Path(__file__).parent / "fixtures/desktop-3-release.json").read_text(encoding="utf-8"))

    def test_release_pair_uses_mac_odd_and_windows_even_versions(self):
        release = self.build.validate_release(self.legacy_release)
        self.assertEqual(release["pairedMacVersion"], "0.1.9")
        self.assertEqual(release["windowsVersion"], "0.1.10")
        self.assertEqual(release["featureLevel"], "desktop-3")

    def test_windows_metadata_reuses_the_shared_feature_contract(self):
        metadata = self.build.windows_metadata(self.legacy_release)
        self.assertEqual(metadata, {
            "name": "零售通",
            "version": "0.1.10",
            "platform": "windows",
            "featureLevel": "desktop-3",
            "pairedMacVersion": "0.1.9",
        })

    def test_desktop4_windows_release_uses_the_shared_feature_contract(self):
        self.assertEqual(self.release["featureLevel"], "desktop-4")
        mac = tuple(map(int, self.release["macVersion"].split(".")))
        windows = tuple(map(int, self.release["windowsVersion"].split(".")))
        self.assertEqual(windows[:2], mac[:2])
        self.assertGreater(windows[2], mac[2])
        self.assertEqual(windows[2] % 2, 0)
        checked = self.build.validate_release(self.release)
        self.assertEqual(checked["featureLevel"], "desktop-4")
        metadata = self.build.windows_metadata(self.release)
        self.assertEqual(metadata["version"], self.release["windowsVersion"])
        self.assertEqual(metadata["featureLevel"], "desktop-4")
        self.assertEqual(metadata["pairedMacVersion"], "0.2.1")

    def test_unsupported_feature_level_is_rejected(self):
        unsupported = dict(self.release, featureLevel="desktop-2")
        with self.assertRaisesRegex(ValueError, "desktop-3.*desktop-4"):
            self.build.validate_release(unsupported)

    def test_source_layout_separates_app_installer_and_uninstaller(self):
        expected = [
            "app/RetailLink.Windows.csproj",
            "app/Program.cs",
            "app/MainForm.cs",
            "app/BackendManager.cs",
            "app/NativeBridge.cs",
            "installer/RetailLink.Setup.csproj",
            "installer/Program.cs",
            "installer/InstallDirectoryGuard.cs",
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

    def test_installer_and_uninstaller_use_registered_location_and_preserve_documents(self):
        installer = (WINDOWS / "installer" / "Program.cs").read_text(encoding="utf-8")
        uninstaller = (WINDOWS / "uninstall" / "uninstall.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("零售通", installer)
        self.assertIn("InstallLocation", installer)
        self.assertIn("UninstallString", installer)
        self.assertIn("InstallLocation", uninstaller)
        self.assertIn("Documents", uninstaller)
        self.assertIn("普通卸载", uninstaller)
        self.assertIn("彻底卸载", uninstaller)
        self.assertNotIn("Remove-Item -LiteralPath $documents -Recurse", uninstaller)
        complete_targets = uninstaller.split("$completeTargets = @(", 1)[1].split(")", 1)[0]
        for target in ("'logs'", "'日志'", "'WebView2'", "'operations\\state.json'",
                       "'writeback\\settings.json'", "'writeback\\templates.json'"):
            self.assertIn(target, complete_targets)
        self.assertNotIn("'writeback'", complete_targets)
        self.assertNotIn("'operations'", complete_targets)

    def test_installer_has_real_path_choice_shortcut_choice_and_product_completion(self):
        installer = (WINDOWS / "installer" / "Program.cs").read_text(encoding="utf-8")
        for text in (
            "选择安装位置",
            "浏览…",
            "立即启动零售通",
            "创建桌面快捷方式",
            "创建开始菜单快捷方式",
            "安装完成",
            "首次使用",
            "本地数据与联网说明",
            "打开安装目录",
            "LaunchAfterInstall",
            "CreateDesktopShortcut",
            "CreateStartMenuShortcut",
            "ValidateInstallDirectory",
        ):
            self.assertIn(text, installer)
        self.assertNotIn("安装目录必须位于当前用户的 LocalAppData", installer)
        self.assertNotIn("安装目录名称必须为“零售通”", installer)

    def test_installer_docks_header_before_content_and_keeps_footer_visible(self):
        installer = (WINDOWS / "installer" / "Program.cs").read_text(encoding="utf-8")

        def page_method(name):
            prefixes = (f"private void {name}(", f"private async Task {name}(")
            starts = [installer.find(prefix) for prefix in prefixes]
            starts = [start for start in starts if start >= 0]
            self.assertTrue(starts, f"未找到安装器页面方法：{name}")
            start = min(starts)
            end = installer.find("\n    private ", start + 1)
            if end < 0:
                end = installer.find("\n    internal ", start + 1)
            return installer[start:end if end >= 0 else None]

        options = page_method("ShowOptionsPage")
        for expected in (
            "page.Controls.Add(footer);",
            "page.Controls.Add(body);",
            "page.Controls.Add(description);",
            "page.Controls.Add(header);",
        ):
            self.assertIn(expected, options)
        self.assertLess(
            options.index("page.Controls.Add(body);"),
            options.index("page.Controls.Add(header);"),
        )
        self.assertLess(
            options.index("page.Controls.Add(footer);"),
            options.index("page.Controls.Add(header);"),
        )

        for name in ("ShowCompletePage", "ShowFailurePage"):
            page = page_method(name)
            self.assertLess(
                page.index("page.Controls.Add(body);"),
                page.index("page.Controls.Add(header);"),
            )
            self.assertLess(
                page.index("page.Controls.Add(footer);"),
                page.index("page.Controls.Add(header);"),
            )

        installing = page_method("BeginInstallAsync")
        self.assertLess(
            installing.index("page.Controls.Add(layout);"),
            installing.index("page.Controls.Add(header);"),
        )

    def test_installer_recovers_existing_location_and_checks_writability_and_space(self):
        installer = (WINDOWS / "installer" / "Program.cs").read_text(encoding="utf-8")
        for text in (
            "InstallLocation",
            "ExistingInstallDirectory",
            "DriveInfo",
            "AvailableFreeSpace",
            "可写",
            "旧版",
        ):
            self.assertIn(text, installer)

    def test_installer_uses_guard_before_extracting_into_existing_directory(self):
        installer = (WINDOWS / "installer" / "Program.cs").read_text(encoding="utf-8")
        guard = (WINDOWS / "installer" / "InstallDirectoryGuard.cs").read_text(encoding="utf-8")
        combined = installer + guard
        start = installer.index("EnsureApplicationStopped();")
        extract = installer.index("ExtractPayload(payload, staging);", start)
        guarded = installer[start:extract]
        self.assertIn("InstallDirectoryGuard.ValidateExistingDirectory", combined)
        self.assertIn("InstallDirectoryGuard.VerifyExistingDirectoryCanBeReplaced", guarded)
        for text in ("安装位置不可替换", "安装前目录占用/权限检查", "其他文件"):
            self.assertIn(text, combined)

    def test_installer_guard_preserves_user_files_and_detects_locked_directories(self):
        dotnet = shutil.which("dotnet")
        if not dotnet:
            self.skipTest(".NET SDK 不可用")
        guard_source = WINDOWS / "installer" / "InstallDirectoryGuard.cs"
        self.assertTrue(guard_source.is_file(), "缺少安装目录安全检查模块")
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder) / "guard-tests"
            project.mkdir()
            linked_source = html.escape(str(guard_source).replace("\\", "/"))
            (project / "GuardTests.csproj").write_text(
                "<Project Sdk=\"Microsoft.NET.Sdk\">\n"
                "  <PropertyGroup>\n"
                "    <OutputType>Exe</OutputType>\n"
                "    <TargetFramework>net10.0-windows</TargetFramework>\n"
                "    <ImplicitUsings>enable</ImplicitUsings>\n"
                "    <Nullable>enable</Nullable>\n"
                "  </PropertyGroup>\n"
                "  <ItemGroup>\n"
                f"    <Compile Include=\"{linked_source}\" Link=\"InstallDirectoryGuard.cs\" />\n"
                "  </ItemGroup>\n"
                "</Project>\n",
                encoding="utf-8",
            )
            (project / "Program.cs").write_text(
                """
using RetailLink.Setup;

static class Harness
{
    private static void ExpectFailure(Action action, params string[] expected)
    {
        try
        {
            action();
        }
        catch (InvalidOperationException error)
        {
            foreach (var text in expected)
            {
                if (!error.Message.Contains(text, StringComparison.Ordinal))
                {
                    throw new InvalidOperationException($"错误提示缺少：{text}\\n{error.Message}", error);
                }
            }
            return;
        }
        throw new InvalidOperationException("预期安装目录检查失败，但实际通过。");
    }

    public static int Main()
    {
        var root = Path.Combine(Path.GetTempPath(), "retail-guard-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        try
        {
            var userDirectory = Path.Combine(root, "用户 软件");
            Directory.CreateDirectory(userDirectory);
            var userFile = Path.Combine(userDirectory, "IntelliJ-配置.txt");
            File.WriteAllText(userFile, "不得移动或删除");
            ExpectFailure(
                () => InstallDirectoryGuard.ValidateExistingDirectory(userDirectory),
                userDirectory, "其他文件", "浏览");
            if (!File.Exists(userFile) || File.ReadAllText(userFile) != "不得移动或删除")
            {
                throw new InvalidOperationException("未知用户目录被修改。");
            }

            var emptyDirectory = Path.Combine(root, "空 目录");
            Directory.CreateDirectory(emptyDirectory);
            InstallDirectoryGuard.ValidateExistingDirectory(emptyDirectory);
            InstallDirectoryGuard.VerifyExistingDirectoryCanBeReplaced(emptyDirectory);
            if (!Directory.Exists(emptyDirectory))
            {
                throw new InvalidOperationException("空目录替换检查后未恢复。");
            }

            var retailDirectory = Path.Combine(root, "零售通");
            Directory.CreateDirectory(Path.Combine(retailDirectory, "app"));
            File.WriteAllText(Path.Combine(retailDirectory, "零售通.exe"), "");
            File.WriteAllText(Path.Combine(retailDirectory, "uninstall.ps1"), "");
            File.WriteAllText(
                Path.Combine(retailDirectory, "app", "版本.json"),
                "{\\"name\\":\\"零售通\\",\\"platform\\":\\"windows\\",\\"version\\":\\"0.2.4\\"}");
            InstallDirectoryGuard.ValidateExistingDirectory(retailDirectory);
            InstallDirectoryGuard.VerifyExistingDirectoryCanBeReplaced(retailDirectory);

            var lockedDirectory = Path.Combine(root, "被占用目录");
            Directory.CreateDirectory(lockedDirectory);
            var lockedFile = Path.Combine(lockedDirectory, "held.txt");
            File.WriteAllText(lockedFile, "x");
            using (new FileStream(lockedFile, FileMode.Open, FileAccess.Read, FileShare.Read))
            {
                ExpectFailure(
                    () => InstallDirectoryGuard.VerifyExistingDirectoryCanBeReplaced(lockedDirectory),
                    lockedDirectory, "安装位置不可替换", "占用");
            }
            if (!Directory.Exists(lockedDirectory))
            {
                throw new InvalidOperationException("锁定目录检查后原目录丢失。");
            }

            var futureChinesePath = Path.Combine(root, "新 中文 路径", "零售通");
            InstallDirectoryGuard.ValidateExistingDirectory(futureChinesePath);
            InstallDirectoryGuard.VerifySiblingDirectoryRename(root);
            Console.Write("PASS");
            return 0;
        }
        finally
        {
            if (Directory.Exists(root))
            {
                Directory.Delete(root, true);
            }
        }
    }
}
""".lstrip(),
                encoding="utf-8",
            )
            result = subprocess.run(
                [dotnet, "run", "--project", str(project), "-c", "Release"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_uninstaller_resolves_registry_location_and_has_explicit_scope_ui(self):
        uninstaller = (WINDOWS / "uninstall" / "uninstall.ps1").read_text(encoding="utf-8-sig")
        for text in (
            "InstallLocation",
            "GetValue",
            "普通卸载",
            "彻底卸载",
            "RadioButton",
            "二次确认",
            "卸载结果",
            "已删除",
            "已保留",
            "失败项",
            "日志位置",
            "exit 1",
            "UninstallRecord",
        ):
            self.assertIn(text, uninstaller)
        self.assertNotIn("拒绝卸载非标准安装目录", uninstaller)
        self.assertNotIn("YesNoCancel", uninstaller)

    def test_uninstaller_docks_header_last_for_scope_and_result_windows(self):
        uninstaller = (WINDOWS / "uninstall" / "uninstall.ps1").read_text(encoding="utf-8-sig")
        for name in ("Show-ScopeForm", "Show-ResultForm"):
            start = uninstaller.index(f"function {name}")
            end = uninstaller.find("\nfunction ", start + 1)
            section = uninstaller[start:end if end >= 0 else None]
            self.assertLess(
                section.index("$form.Controls.Add($footer)"),
                section.index("$form.Controls.Add($header)"),
            )

    def test_startup_reports_stages_and_handles_frontend_ready_without_id(self):
        main = (WINDOWS / "app" / "MainForm.cs").read_text(encoding="utf-8")
        bridge = (WINDOWS / "app" / "NativeBridge.cs").read_text(encoding="utf-8")
        combined = main + bridge
        for text in (
            "运行环境",
            "后端就绪",
            "页面加载",
            "可操作",
            "显示启动诊断",
            "启动诊断.log",
            "frontendReady",
            "启动超时",
            "记录启动阶段",
        ):
            self.assertIn(text, combined)
        self.assertIn("action == \"frontendReady\"", bridge)

    def test_windows_manual_is_windows_specific_and_packaged(self):
        manual = WINDOWS / "使用说明.html"
        self.assertTrue(manual.is_file())
        html = manual.read_text(encoding="utf-8")
        self.assertIn("Windows 试用版", html)
        # A new Mac release may reserve the next Windows version without
        # rewriting that platform's previously built manual.
        self.assertIn(self.release.get("lastBuiltWindowsVersion", self.release["windowsVersion"]), html)
        self.assertNotIn("MAC 试用版", html)
        source = (WINDOWS / "build_windows.py").read_text(encoding="utf-8")
        self.assertIn('WINDOWS / "使用说明.html"', source)
        self.assertNotIn('shutil.copy2(DESKTOP / "使用说明.html", app / "使用说明.html")', source)

    def test_generated_icon_uses_red_chart_branding(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "RetailLink.ico"
            self.build.write_icon(path)
            data = path.read_bytes()
            self.assertEqual(struct.unpack("<HHH", data[:6]), (0, 1, 1))
            directory_size = struct.unpack("<I", data[14:18])[0]
            png = data[22:22 + directory_size]
            width, height = struct.unpack(">II", png[16:24])
            offset = 8
            idat = bytearray()
            while offset < len(png):
                length = struct.unpack(">I", png[offset:offset + 4])[0]
                kind = png[offset + 4:offset + 8]
                payload = png[offset + 8:offset + 8 + length]
                if kind == b"IDAT":
                    idat.extend(payload)
                offset += 12 + length
            pixels = zlib.decompress(bytes(idat))
            stride = width * 4 + 1
            rgba = pixels[128 * stride + 1 + 128 * 4:128 * stride + 5 + 128 * 4]
            self.assertGreater(width, 64)
            self.assertEqual(height, width)
            self.assertGreater(rgba[0], rgba[1] + 40, rgba)
            self.assertGreater(rgba[0], rgba[2] + 40, rgba)
            red_pixels = 0
            light_pixels = 0
            for row in range(height):
                start = row * stride + 1
                for column in range(width):
                    red, green, blue, alpha = pixels[start + column * 4:start + column * 4 + 4]
                    if alpha > 200 and red > green + 60 and red > blue + 40:
                        red_pixels += 1
                    if alpha > 200 and min(red, green, blue) > 220:
                        light_pixels += 1
            self.assertGreater(red_pixels, 5000)
            self.assertGreater(light_pixels, 20000)

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
