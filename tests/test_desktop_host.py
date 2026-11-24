"""Desktop backend contract, exercised with isolated read-only app bundles.

No customer data, real Application Support folders, or Documents are used.
"""
from __future__ import annotations

from contextlib import redirect_stderr
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import openpyxl
from tests.temp_support import temporary_directory

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT
TEST_DIR = Path(__file__).resolve().parent
WINDOWS = os.name == "nt"
sys.path.insert(0, str(DEV))
spec = importlib.util.spec_from_file_location("desktop_host_contract", DEV / "server.py")
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


class DesktopHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory(prefix="desktop-host-", directory=TEST_DIR)
        self.folder = Path(self.temp.name)
        self.bundle = self.folder / "只读应用" / "零售通.app"
        self.app = self.bundle / "Contents" / "Resources" / "app"
        self.app.mkdir(parents=True)
        shutil.copy2(DEV / "server.py", self.app / "server.py")
        for name in ("backend", "web"):
            shutil.copytree(DEV / name, self.app / name,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
        self.manifest = self.app / "版本.json"
        self.manifest.write_text(json.dumps({"version": "0.1.5", "platform": "mac", "featureLevel": "desktop-1"}))
        self.data = self.folder / "用户数据" / "零售通"
        self.originals = self.folder / "用户选择" / "原始资料"
        self.output = self.folder / "用户文档" / "零售通" / "生成文件"
        self.ready = self.folder / "服务状态" / "ready.json"
        self.processes = []
        self.resource_hashes = self.hashes()
        for path in sorted(self.bundle.rglob("*"), reverse=True):
            path.chmod(0o555 if path.is_dir() else 0o444)
        self.bundle.chmod(0o555)

    def tearDown(self):
        for process, stream in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=8)
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            stream.close()
        for path in [self.bundle, *self.bundle.rglob("*")]:
            if path.is_dir():
                path.chmod(0o755)
        self.temp.cleanup()

    def hashes(self):
        return {p.relative_to(self.bundle).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.bundle.rglob("*") if p.is_file()}

    def command(self, **changes):
        values = {"data-dir": self.data, "original-dir": self.originals,
                  "export-dir": self.output, "ready-file": self.ready, "port": 0, **changes}
        command = [sys.executable, "-B", str(self.app / "server.py"), "--desktop"]
        for key, value in values.items():
            if value is not None:
                command.extend(["--" + key] if value is True else ["--" + key, str(value)])
        return command

    def launch(self, **changes):
        stream = (self.folder / f"process-{len(self.processes)}.log").open("w+")
        process = subprocess.Popen(self.command(**changes), cwd="/", stdin=subprocess.PIPE, stdout=stream, stderr=stream)
        self.processes.append((process, stream))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stream.seek(0)
                self.fail("服务未就绪就退出：" + stream.read())
            try:
                ready = json.loads(self.ready.read_text())
                if ready.get("pid") == process.pid:
                    return process, ready
            except (OSError, ValueError):
                pass
            time.sleep(.03)
        self.fail("服务未在20秒内写入就绪信息。")

    def call(self, ready, route, body=None):
        request = Request(ready["url"] + route,
                          data=json.dumps(body).encode() if body is not None else None,
                          headers={"Content-Type": "application/json"} if body is not None else {})
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def success(self, ready, route, body=None):
        status, value = self.call(ready, route, body)
        self.assertEqual(status, 200, value)
        return value

    def stop(self, process):
        process.terminate()
        expected = 1 if WINDOWS else 0
        self.assertEqual(process.wait(timeout=8), expected)

    def test_port_zero_ready_metadata_and_read_only_bundle(self):
        process, ready = self.launch()
        process.stdin.close()  # Without --parent-stdin, EOF must not stop a CLI host.
        self.assertEqual(set(ready), {"url", "version", "pid"})
        self.assertEqual(ready["pid"], process.pid)
        self.assertGreater(urlparse(ready["url"]).port, 0)
        self.assertEqual(ready["version"], "0.1.5")
        health = self.success(ready, "/api/health")
        self.assertTrue(health["desktop"])
        self.assertEqual(health["platform"], "mac")
        self.assertEqual(health["featureLevel"], "desktop-1")
        self.assertEqual(health["defaultDirectory"], str(self.originals))
        self.assertEqual(health["defaultExportDirectory"], str(self.output))
        self.assertTrue(self.data.is_dir())
        self.assertTrue(self.originals.is_dir())
        self.assertTrue(self.output.is_dir())
        self.assertFalse(list(self.ready.parent.glob(".retail-ready-*")))
        self.assertEqual(self.hashes(), self.resource_hashes)
        self.assertFalse((self.app.parent / "生成文件").exists())
        self.assertFalse((self.app.parent / "原始资料").exists())
        self.stop(process)
        if not WINDOWS:
            self.assertFalse(self.ready.exists())

    def test_preferences_survive_new_port_and_backend_process(self):
        process, ready = self.launch()
        preferences = {"version": 1, "profiles": [{"name": "虚构店铺", "config": {"targetRate": .85}}],
                       "appearance": {"mode": "dark", "palette": "focus", "glass": False},
                       "dailyMapping": {"product": "A", "price": "B"}}
        self.success(ready, "/api/preferences", {"preferences": preferences})
        old_port = urlparse(ready["url"]).port
        self.stop(process)
        with socket.socket() as occupied:
            occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            occupied.bind(("127.0.0.1", old_port))
            occupied.listen()
            second, second_ready = self.launch()
            self.assertNotEqual(urlparse(second_ready["url"]).port, old_port)
            self.assertEqual(self.success(second_ready, "/api/preferences")["preferences"], preferences)
            self.stop(second)
        self.assertEqual(self.hashes(), self.resource_hashes)

    def test_custom_export_directory_works_while_app_and_data_are_protected(self):
        process, ready = self.launch()
        source = self.originals / "虚构月计划.xlsx"
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "月计划"
        sheet.append(["商品", "月计划"])
        sheet.append(["虚构商品001", 1000])
        book.save(source)
        book.close()
        opened = self.success(ready, "/api/open", {"path": str(source)})
        result = self.success(ready, "/api/analyze", {"workbookId": opened["workbookId"], "config": opened["config"]})
        request = {"snapshotId": result["id"], "targetDir": str(self.output), "filename": "虚构看盘.xlsx"}
        preview = self.success(ready, "/api/export/preview", request)
        exported = self.success(ready, "/api/export/xlsx", {"token": preview["token"]})
        self.assertEqual(Path(exported["path"]).parent, self.output)
        self.assertTrue(Path(exported["path"]).is_file())
        self.success(ready, "/api/export/logs", {"path": str(self.output / "操作日志.jsonl")})
        self.success(ready, "/api/data/bundle", {"path": str(self.output / "迁移包.zip")})
        for target in (self.app, self.bundle / "Contents" / "Resources", self.data, self.originals):
            request["targetDir"] = str(target)
            self.assertEqual(self.call(ready, "/api/export/preview", request)[0], 400)
            self.assertEqual(self.call(ready, "/api/export/logs", {"path": str(target / "禁止日志.jsonl")})[0], 400)
            self.assertEqual(self.call(ready, "/api/data/bundle", {"path": str(target / "禁止迁移.zip")})[0], 400)
        self.assertEqual(self.hashes(), self.resource_hashes)
        self.stop(process)

    def test_foreign_ready_file_is_not_removed_on_exit(self):
        process, _ = self.launch()
        foreign = {"url": "http://127.0.0.1:1", "version": "other", "pid": process.pid + 100000}
        self.ready.write_text(json.dumps(foreign))
        self.stop(process)
        self.assertEqual(json.loads(self.ready.read_text()), foreign)

    def test_parent_pipe_eof_stops_service_and_removes_ready(self):
        process, ready = self.launch(**{"parent-stdin": True})
        self.assertTrue(self.success(ready, "/api/health")["desktop"])
        process.stdin.write(b"keep-open")
        process.stdin.flush()
        self.assertIsNone(process.poll())
        self.assertEqual(self.success(ready, "/api/health")["version"], "0.1.5")
        process.stdin.close()
        self.assertEqual(process.wait(timeout=8), 0)
        self.assertFalse(self.ready.exists())

    def test_desktop_refuses_writable_locations_inside_app_bundle(self):
        stream = (self.folder / "invalid.log").open("w+")
        process = subprocess.Popen(self.command(**{"data-dir": self.app / "运行数据"}), stdout=stream, stderr=stream)
        self.processes.append((process, stream))
        self.assertEqual(process.wait(timeout=15), 1)
        self.assertFalse(self.ready.exists())
        self.assertEqual(self.hashes(), self.resource_hashes)

    def test_ready_is_not_written_when_binding_fails(self):
        options = self.command()[3:]
        with mock.patch.object(host, "APP_DIR", self.app), mock.patch.object(host, "ThreadingHTTPServer", side_effect=OSError("test bind failure")) as server:
            with redirect_stderr(io.StringIO()):
                self.assertEqual(host.main(options), 1)
        server.assert_called_once()
        self.assertFalse(self.ready.exists())

    def test_windows_metadata_uses_same_feature_contract(self):
        self.manifest.chmod(0o644)
        self.manifest.write_text(json.dumps({"version": "0.1.6", "platform": "windows", "featureLevel": "desktop-1"}))
        self.manifest.chmod(0o444)
        process, ready = self.launch()
        self.assertEqual(ready["version"], "0.1.6")
        health = self.success(ready, "/api/health")
        self.assertEqual(health["version"], "0.1.6")
        self.assertEqual(health["platform"], "windows")
        self.assertEqual(health["featureLevel"], "desktop-1")
        self.stop(process)

    def test_default_user_paths_do_not_depend_on_bundle_location(self):
        fake_home = self.folder / "fake-home"
        mac = host.desktop_directories(home=fake_home, platform="mac", environ={})
        self.assertEqual(mac["data"], fake_home / "Library/Application Support/零售通")
        self.assertEqual(mac["original"], mac["data"] / "原始资料")
        self.assertEqual(mac["export"], fake_home / "Documents/零售通/生成文件")
        windows = host.desktop_directories(home=fake_home, platform="windows", environ={"LOCALAPPDATA": str(self.folder / "LocalAppData")})
        self.assertEqual(windows["data"], self.folder / "LocalAppData/零售通")
        self.assertFalse(fake_home.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
