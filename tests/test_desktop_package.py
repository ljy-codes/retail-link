"""Verify an already-built desktop app without opening its native window.

Run this file directly with --app /path/to/零售通.app to verify another build.
Writable test data and the read-only relocation fixture remain in 测试空间.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import time
import unittest
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from tests.temp_support import temporary_directory


ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT
TEST_DIR = Path(__file__).resolve().parent
APP = DEV / "desktop/build/macos/零售通.app"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def file_map(directory):
    return {p.relative_to(directory).as_posix(): digest(p)
            for p in directory.rglob("*") if p.is_file()}


def clean_environment(home, temporary):
    # Explicitly omit PYTHONHOME/PYTHONPATH and any outer interpreter paths.
    return {"PATH": "/usr/bin:/bin", "HOME": str(home), "TMPDIR": str(temporary),
            "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1", "LANG": "en_US.UTF-8"}


class DesktopPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = APP.expanduser().resolve()
        if not cls.bundle.is_dir():
            raise unittest.SkipTest("源码不含构建App，先构建或用 --app 指定后验证：" + str(cls.bundle))
        cls.resources = cls.bundle / "Contents/Resources"
        cls.application = cls.resources / "app"
        cls.runtime = cls.resources / "runtime"
        cls.python = cls.runtime / "bin/python3"
        cls.release = json.loads((DEV / "desktop/release.json").read_text())
        cls.metadata = json.loads((cls.application / "版本.json").read_text())

    def test_embedded_python_is_self_contained_and_executes_actual_dependencies(self):
        with temporary_directory(prefix="package-runtime-", directory=TEST_DIR) as directory:
            folder = Path(directory)
            home, temporary = folder / "用户目录", folder / "临时目录"
            home.mkdir()
            temporary.mkdir()
            code = """
import sys,os,sysconfig,json,io,sqlite3,ssl,_sqlite3,_ssl
import openpyxl,et_xmlfile
book=openpyxl.Workbook();book.active['A1']='000001'
payload=io.BytesIO();book.save(payload);book.close();payload.seek(0)
read=openpyxl.load_workbook(payload);excel_value=read.active['A1'].value;read.close()
database=sqlite3.connect(':memory:')
database.execute('create table verification(value integer)')
database.execute('insert into verification values (7)')
sqlite_value=database.execute('select value from verification').fetchone()[0];database.close()
context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
packages=sysconfig.get_path('purelib')
third_party=sorted({name.split('.')[0] for name,module in sys.modules.items()
 if getattr(module,'__file__',None) and os.path.realpath(module.__file__).startswith(os.path.realpath(packages)+os.sep)})
print(json.dumps({'prefix':sys.prefix,'basePrefix':sys.base_prefix,'executable':sys.executable,
 'path':sys.path,'packages':packages,'thirdParty':third_party,
 'moduleFiles':{m.__name__:getattr(m,'__file__',sys.executable) for m in (openpyxl,et_xmlfile,sqlite3,ssl,_sqlite3,_ssl)},
 'openpyxlVersion':openpyxl.__version__,'sqlite':sqlite3.sqlite_version,'ssl':ssl.OPENSSL_VERSION,
 'excelValue':excel_value,'sqliteValue':sqlite_value,'tlsClient':context.protocol==ssl.PROTOCOL_TLS_CLIENT,
 'hasExternalPythonEnvironment':any(key in os.environ for key in ('PYTHONHOME','PYTHONPATH'))}))
"""
            result = subprocess.run([str(self.python), "-I", "-B", "-c", code],
                                    cwd=folder, env=clean_environment(home, temporary),
                                    check=True, capture_output=True, text=True, timeout=20)
            probe = json.loads(result.stdout)
            self.assertEqual(Path(probe["prefix"]).resolve(), self.runtime)
            self.assertEqual(Path(probe["basePrefix"]).resolve(), self.runtime)
            self.assertEqual(Path(probe["executable"]).resolve(), self.python)
            self.assertFalse(probe["hasExternalPythonEnvironment"])
            self.assertTrue(all(Path(path).resolve().is_relative_to(self.runtime) for path in probe["path"]))
            self.assertEqual(set(probe["thirdParty"]), {"openpyxl", "et_xmlfile"})
            self.assertTrue(all(Path(path).resolve().is_relative_to(self.runtime) for path in probe["moduleFiles"].values()))
            self.assertEqual(probe["excelValue"], "000001")
            self.assertEqual(probe["sqliteValue"], 7)
            self.assertTrue(probe["tlsClient"])
            self.assertTrue(probe["sqlite"])
            self.assertIn("OpenSSL", probe["ssl"])
            self.assertEqual(probe["openpyxlVersion"], "3.1.5")

    def test_info_release_versions_and_feature_level_match(self):
        info = plistlib.loads((self.bundle / "Contents/Info.plist").read_bytes())
        mac = self.release["macVersion"]
        windows = self.release["windowsVersion"]
        mac_parts, windows_parts = mac.split("."), windows.split(".")
        self.assertEqual(mac_parts[:-1], windows_parts[:-1])
        self.assertEqual(int(mac_parts[-1]) % 2, 1)
        self.assertEqual(int(windows_parts[-1]) % 2, 0)
        self.assertGreater(int(windows_parts[-1]), int(mac_parts[-1]))
        self.assertEqual(info["CFBundleShortVersionString"], mac)
        self.assertEqual(info["CFBundleVersion"], mac_parts[-1])
        self.assertEqual(info["LSMinimumSystemVersion"], self.release["minimumMacOS"])
        self.assertEqual(info["CFBundlePackageType"], "APPL")
        self.assertEqual(self.metadata["version"], mac)
        self.assertEqual(self.metadata["pairedWindowsVersion"], windows)
        self.assertEqual(self.metadata["platform"], "mac")
        self.assertEqual(self.metadata["featureLevel"], self.release["featureLevel"])
        self.assertEqual(json.loads((self.application / "功能清单.json").read_text()), self.release["features"])
        self.assertTrue((self.bundle / "Contents/MacOS" / info["CFBundleExecutable"]).is_file())
        self.assertTrue(os.access(self.python, os.X_OK))

    def test_packaged_server_backend_and_web_match_development_sources(self):
        from desktop.shared_payload import verify_shared_payload
        verify_shared_payload(self.application)
        self.assertEqual(digest(self.application / "server.py"), digest(DEV / "server.py"))
        for name in ("backend", "web"):
            expected = {p.relative_to(DEV / name).as_posix(): digest(p)
                        for p in (DEV / name).rglob("*")
                        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc" and p.name != ".DS_Store"}
            self.assertEqual(file_map(self.application / name), expected, name + " 与开发源码不同，请重新打包。")
        self.assertEqual(digest(self.resources / "使用说明.html"), digest(DEV / "desktop/使用说明.html"))

    def test_bundle_contains_only_application_runtime_and_no_user_or_test_data(self):
        self.assertEqual({p.name for p in self.application.iterdir()},
                          {"server.py", "backend", "web", "版本.json", "功能清单.json",
                           "shared-source-manifest.json"})
        forbidden = {"原始资料", "本地数据", "生成文件", "测试空间", "测试数据", "导入记录", "history",
                     "history_deleted", "import_sessions", "__pycache__", ".pytest_cache", ".git", "node_modules"}
        unwanted_suffixes = {".pyc", ".pyo", ".xlsx", ".xlsm", ".xls", ".sqlite3", ".db", ".jsonl", ".csv", ".dmg"}
        unwanted = []
        for path in self.bundle.rglob("*"):
            relative = path.relative_to(self.bundle)
            if set(relative.parts) & forbidden or path.suffix.lower() in unwanted_suffixes or path.name == ".DS_Store":
                unwanted.append(relative.as_posix())
            if path.is_symlink():
                self.assertTrue(path.resolve().is_relative_to(self.bundle), "应用包包含外部运行依赖：" + str(relative))
        self.assertEqual(unwanted, [])
        site_packages = list((self.runtime / "lib").glob("python*/site-packages"))
        self.assertEqual(len(site_packages), 1)
        children = list(site_packages[0].iterdir())
        package_names = {p.name for p in children if p.is_dir() and not p.name.endswith(".dist-info")}
        self.assertEqual(package_names, {"openpyxl", "et_xmlfile"})
        for path in children:
            self.assertTrue(path.name in {"openpyxl", "et_xmlfile"}
                            or path.name.startswith(("openpyxl-", "et_xmlfile-")) and path.name.endswith(".dist-info"), path.name)

    def test_relocated_read_only_bundle_starts_saves_preferences_and_cleans_ready(self):
        with temporary_directory(prefix="package-readonly-", directory=TEST_DIR) as directory:
            folder = Path(directory)
            relocated = folder / "含空格 中文路径" / self.bundle.name
            shutil.copytree(self.bundle, relocated, symlinks=True)
            resources = relocated / "Contents/Resources"
            python = resources / "runtime/bin/python3"
            server = resources / "app/server.py"
            home, temporary = folder / "用户目录", folder / "临时目录"
            home.mkdir()
            temporary.mkdir()
            data, original, output = folder / "运行数据", folder / "输入副本", folder / "导出结果"
            ready = folder / "就绪状态/backend-ready.json"
            before = file_map(relocated)
            original_modes = {}
            for path in [relocated, *relocated.rglob("*")]:
                if path.is_symlink():
                    continue
                original_modes[path] = stat.S_IMODE(path.stat().st_mode)
                path.chmod(0o555 if path.is_dir() or os.access(path, os.X_OK) else 0o444)
            process = None
            try:
                self.assertFalse(server.stat().st_mode & stat.S_IWUSR)
                self.assertFalse(resources.stat().st_mode & stat.S_IWUSR)
                with (folder / "backend.log").open("w+") as log:
                    process = subprocess.Popen([str(python), "-B", str(server), "--desktop", "--parent-stdin", "--port", "0",
                                                "--data-dir", str(data), "--original-dir", str(original),
                                                "--export-dir", str(output), "--ready-file", str(ready)],
                                               cwd=folder, stdin=subprocess.PIPE, stdout=log, stderr=log,
                                               env=clean_environment(home, temporary))
                    deadline = time.monotonic() + 20
                    payload = None
                    while time.monotonic() < deadline:
                        if process.poll() is not None:
                            log.seek(0)
                            self.fail("内置运行环境未就绪：" + log.read())
                        try:
                            candidate = json.loads(ready.read_text())
                            if candidate.get("pid") == process.pid:
                                payload = candidate
                                break
                        except (OSError, ValueError):
                            pass
                        time.sleep(.03)
                    self.assertIsNotNone(payload, "内置后端未写入就绪文件。")
                    self.assertEqual(payload["version"], self.release["macVersion"])
                    self.assertGreater(urlparse(payload["url"]).port, 0)
                    with urlopen(payload["url"] + "/api/health", timeout=10) as response:
                        health = json.loads(response.read())
                    self.assertTrue(health["desktop"])
                    self.assertEqual(health["platform"], "mac")
                    self.assertEqual(health["version"], self.release["macVersion"])
                    self.assertEqual(health["featureLevel"], self.release["featureLevel"])
                    self.assertEqual(health["defaultDirectory"], str(original))
                    self.assertEqual(health["defaultExportDirectory"], str(output))
                    preferences = {"version": 1, "profiles": [{"name": "虚构验证", "config": {"targetRate": .85}}],
                                   "appearance": {"mode": "dark", "palette": "focus", "glass": False},
                                   "dailyMapping": {"product": "A", "price": "B"}}
                    request = Request(payload["url"] + "/api/preferences",
                                      data=json.dumps({"preferences": preferences}).encode(),
                                      headers={"Content-Type": "application/json"})
                    with urlopen(request, timeout=10) as response:
                        self.assertEqual(response.status, 200)
                    with urlopen(payload["url"] + "/api/preferences", timeout=10) as response:
                        self.assertEqual(json.loads(response.read())["preferences"], preferences)
                    self.assertEqual(json.loads((data / "preferences.json").read_text()), preferences)
                    self.assertTrue(original.is_dir())
                    self.assertTrue(output.is_dir())
                    process.stdin.close()
                    self.assertEqual(process.wait(timeout=10), 0)
                    self.assertFalse(ready.exists())
                    self.assertEqual(file_map(relocated), before)
            finally:
                if process is not None:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=8)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=8)
                    if process.stdin is not None and not process.stdin.closed:
                        process.stdin.close()
                for path, mode in original_modes.items():
                    path.chmod(mode)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--app", type=Path, default=APP)
    options, remaining = parser.parse_known_args()
    APP = options.app
    unittest.main(argv=[str(Path(__file__)), *remaining])
