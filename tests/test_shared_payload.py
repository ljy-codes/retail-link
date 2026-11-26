"""Real filesystem contracts for shared desktop payloads; no installers are run."""
import importlib.util
import io
import json
from pathlib import Path
import struct
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_script(relative):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(path.stem + "_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SharedPayloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="retail-shared-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "源码"
        self.repo.mkdir()
        release_path = ROOT / "desktop/release.json"
        self.assertLessEqual(release_path.stat().st_size, 314572800)
        self.release = json.loads(release_path.read_bytes())
        self.version = self.release["windowsVersion"]
        self.installer_name = f"零售通-v{self.version}-windows-x64-setup.exe"
        self.manual_name = f"零售通-v{self.version}-使用说明.html"
        for relative, content in {
            "server.py": b"print('server')\r\n",
            "backend/__init__.py": b"",
            "backend/business.py": b"VALUE = 1\r\n",
            "web/app.js": b"const value = 1;\r\n",
        }.items():
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    def shared(self):
        path = ROOT / "desktop/shared_payload.py"
        self.assertTrue(path.is_file(), "missing shared payload implementation")
        return load_script("desktop/shared_payload.py")

    def sparse(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as stream:
            stream.truncate(314572801)

    def test_platform_copies_have_identical_bytes_and_verifiable_manifest(self):
        shared = self.shared()
        for platform in ("macOS", "Windows"):
            target = self.root / platform
            shared.copy_shared_payload(self.repo, target, ["dashboard", "data_health"])
            self.assertEqual((target / "server.py").read_bytes(), b"print('server')\r\n")
            self.assertEqual((target / "backend/business.py").read_bytes(), b"VALUE = 1\r\n")
            manifest = json.loads((target / "shared-source-manifest.json").read_bytes())
            self.assertEqual(set(manifest["files"]), {
                "server.py", "backend/__init__.py", "backend/business.py",
                "web/app.js", "功能清单.json",
            })
            self.assertEqual(manifest["files"]["backend/business.py"]["size"], 11)
            shared.verify_shared_payload(target)
        self.assertEqual(
            (self.root / "macOS/shared-source-manifest.json").read_bytes(),
            (self.root / "Windows/shared-source-manifest.json").read_bytes(),
        )
        (self.root / "Windows/backend/business.py").write_bytes(b"VALUE = 2\r\n")
        with self.assertRaises(ValueError):
            shared.verify_shared_payload(self.root / "Windows")

    def test_business_data_is_rejected_before_destination_is_written(self):
        shared = self.shared()
        for name in ("orders.xlsx", "state.json", "daily.sqlite3", "secret.csv"):
            with self.subTest(name=name):
                unwanted = self.repo / "backend" / name
                unwanted.write_bytes(b"private")
                target = self.root / "payload"
                with self.assertRaises(ValueError):
                    shared.copy_shared_payload(self.repo, target, [])
                self.assertFalse(target.exists())
                unwanted.unlink()

    def test_oversize_source_is_rejected_before_any_content_read(self):
        shared = self.shared()
        self.sparse(self.repo / "backend/large.py")
        with mock.patch.object(Path, "open", side_effect=AssertionError("content opened")):
            with self.assertRaisesRegex(ValueError, "300 MiB"):
                shared.copy_shared_payload(self.repo, self.root / "payload", [])
        self.assertFalse((self.root / "payload").exists())

    def test_oversize_ignored_cache_is_not_silently_bypassed(self):
        shared = self.shared()
        self.sparse(self.repo / "backend/__pycache__/large.pyc")
        with self.assertRaisesRegex(ValueError, "300 MiB"):
            shared.copy_shared_payload(self.repo, self.root / "payload", [])

    def test_escaping_link_is_rejected_without_copying_external_content(self):
        shared = self.shared()
        outside = self.root / "outside.py"
        outside.write_bytes(b"private")
        link = self.repo / "backend/link.py"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("symlink privilege unavailable")
        with self.assertRaises(ValueError):
            shared.copy_shared_payload(self.repo, self.root / "payload", [])
        self.assertFalse((self.root / "payload").exists())

    def test_escaping_directory_link_is_rejected(self):
        shared = self.shared()
        outside = self.root / "external-business"
        outside.mkdir()
        (outside / "private.py").write_bytes(b"private")
        link = self.repo / "backend/external"
        if sys.platform == "win32":
            result = subprocess.run([
                "powershell.exe", "-NoProfile", "-Command",
                "New-Item -ItemType Junction -Path '" + str(link).replace("'", "''")
                + "' -Target '" + str(outside).replace("'", "''") + "' | Out-Null",
            ], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        else:
            link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            shared.copy_shared_payload(self.repo, self.root / "payload", [])
        self.assertFalse((self.root / "payload").exists())

    def test_builds_reject_linked_build_parent_before_creating_output(self):
        mac = load_script("desktop/build_macos.py")
        windows = load_script("desktop/windows/build_windows.py")
        outside = self.root / "external"
        outside.mkdir()
        linked = self.root / "linked-build"
        if sys.platform == "win32":
            result = subprocess.run([
                "powershell.exe", "-NoProfile", "-Command",
                "New-Item -ItemType Junction -Path '" + str(linked).replace("'", "''")
                + "' -Target '" + str(outside).replace("'", "''") + "' | Out-Null",
            ], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        else:
            linked.symlink_to(outside, target_is_directory=True)
        native = self.root / "native"
        native.mkdir()
        (native / "零售通.exe").write_bytes(b"fixture")
        desktop = self.repo / "desktop"
        (desktop / "macos").mkdir(parents=True)
        (desktop / "macos/Info.plist").write_bytes(
            b'<?xml version="1.0"?><plist version="1.0"><dict/></plist>')
        with mock.patch.multiple(mac, BUILD=linked, APP=linked / "零售通.app",
                                 DESKTOP=desktop), mock.patch.object(mac, "run"):
            with self.assertRaises(ValueError):
                mac.build(self.root / "unused-runtime")
        self.assertFalse((outside / "零售通.app").exists())
        with mock.patch.object(windows, "BUILD", linked):
            with self.assertRaises(ValueError):
                windows.build_payload(self.version, native, None, None)
        self.assertFalse((outside / "payload").exists())

    def test_runtime_archive_is_preflighted_before_any_extraction(self):
        shared = self.shared()
        for member in ("../outside", "C:/escape", "folder\\..\\escape", "orders.xlsx"):
            with self.subTest(member=member):
                archive = self.root / "runtime.zip"
                with zipfile.ZipFile(archive, "w") as output:
                    output.writestr("good.txt", b"safe")
                    output.writestr(member, b"unsafe")
                with self.assertRaises(ValueError):
                    shared.extract_runtime_zip(archive, self.root / "runtime")
                self.assertFalse((self.root / "runtime").exists())

    def test_oversize_archive_member_is_rejected_without_decompression(self):
        shared = self.shared()
        archive = self.root / "runtime.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("large.bin", b"x")
        data = bytearray(archive.read_bytes())
        offset = data.index(b"PK\x01\x02")
        struct.pack_into("<I", data, offset + 24, 314572801)
        archive.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "300 MiB"):
            shared.extract_runtime_zip(archive, self.root / "runtime")
        self.assertFalse((self.root / "runtime").exists())

    def test_archive_member_limit_applies_before_copy_read_or_hash(self):
        shared = self.shared()
        source = self.root / "runtime-source"
        source.mkdir()
        archive = source / "runtime.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("large.bin", b"x")
        data = bytearray(archive.read_bytes())
        struct.pack_into("<I", data, data.index(b"PK\x01\x02") + 24, 314572801)
        archive.write_bytes(data)
        target = self.root / "copied"
        actions = {
            "copy": lambda: shared.copy_tree(source, target),
            "read": lambda: shared.read_bytes(archive),
            "hash": lambda: shared.file_digest(archive),
        }
        with mock.patch.object(zipfile.ZipFile, "open",
                               side_effect=AssertionError("member decompressed")):
            for operation, action in actions.items():
                with self.subTest(operation=operation):
                    with self.assertRaisesRegex(ValueError, "300 MiB"):
                        action()
        self.assertFalse(target.exists())

    def test_nested_archive_is_checked_before_outer_archive_is_extracted(self):
        shared = self.shared()
        nested = io.BytesIO()
        with zipfile.ZipFile(nested, "w") as output:
            output.writestr("large.bin", b"x")
        data = bytearray(nested.getvalue())
        struct.pack_into("<I", data, data.index(b"PK\x01\x02") + 24, 314572801)
        archive = self.root / "runtime.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("good.txt", b"safe")
            output.writestr("python312.zip", data)
        with self.assertRaisesRegex(ValueError, "300 MiB"):
            shared.extract_runtime_zip(archive, self.root / "runtime")
        self.assertFalse((self.root / "runtime").exists())

    def test_archive_special_files_are_rejected_before_extraction(self):
        shared = self.shared()
        archive = self.root / "runtime.zip"
        info = zipfile.ZipInfo("pipe")
        info.create_system = 3
        info.external_attr = (stat.S_IFIFO | 0o600) << 16
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr(info, b"")
        with self.assertRaises(ValueError):
            shared.extract_runtime_zip(archive, self.root / "runtime")
        self.assertFalse((self.root / "runtime").exists())

    def test_source_case_collisions_are_rejected_before_copy(self):
        shared = self.shared()
        # These Unicode names coexist on Windows but collide under casefold.
        (self.repo / "backend/straße.py").write_bytes(b"one")
        (self.repo / "backend/STRASSE.py").write_bytes(b"two")
        with self.assertRaises(ValueError):
            shared.copy_shared_payload(self.repo, self.root / "payload", [])
        self.assertFalse((self.root / "payload").exists())

    def test_default_delivery_supports_organized_and_ordinary_repositories(self):
        shared = self.shared()
        self.assertEqual(shared.default_delivery_dir(self.repo, "Windows"),
                         self.root / "产品交付/Windows")
        ordinary = self.root / "retail-link"
        self.assertEqual(shared.default_delivery_dir(ordinary, "macOS"),
                         ordinary / "产品交付/macOS")

    def test_publishing_preserves_unknown_files_and_previous_artifacts(self):
        shared = self.shared()
        staging = self.root / "staging"
        staging.mkdir()
        (staging / self.installer_name).write_bytes(b"fixture only")
        (staging / self.manual_name).write_bytes(b"manual")
        target = self.root / "交付 Windows"
        target.mkdir()
        (target / "customer.xlsx").write_bytes(b"private")
        with self.assertRaises(ValueError):
            shared.publish_delivery(staging, target, "Windows")
        self.assertEqual((target / "customer.xlsx").read_bytes(), b"private")
        self.assertEqual(len(list(target.iterdir())), 1)
        (target / "customer.xlsx").unlink()
        shared.publish_delivery(staging, target, "Windows")
        self.assertEqual(len(list(target.iterdir())), 2)
        (staging / self.manual_name).write_bytes(b"updated")
        shared.publish_delivery(staging, target, "Windows")
        self.assertEqual((target / self.manual_name).read_bytes(), b"updated")

    def test_failed_publication_restores_previous_delivery(self):
        shared = self.shared()
        staging = self.root / "staging"
        staging.mkdir()
        name = self.installer_name
        (staging / name).write_bytes(b"new")
        (staging / self.manual_name).write_bytes(b"new manual")
        target = self.root / "delivery"
        target.mkdir()
        (target / name).write_bytes(b"old")
        original_rename = Path.rename

        def fail_new_publication(path, destination):
            if path.name == "new":
                raise OSError("simulated publication failure")
            return original_rename(path, destination)

        with mock.patch.object(Path, "rename", fail_new_publication):
            with self.assertRaises(OSError):
                shared.publish_delivery(staging, target, "Windows")
        self.assertEqual((target / name).read_bytes(), b"old")

    def test_mismatched_manual_version_is_not_published(self):
        shared = self.shared()
        staging = self.root / "staging"
        staging.mkdir()
        (staging / self.installer_name).write_bytes(b"fixture")
        (staging / "零售通-v0.1.10-使用说明.html").write_bytes(b"old manual")
        with self.assertRaises(ValueError):
            shared.publish_delivery(staging, self.root / "delivery", "Windows")
        self.assertFalse((self.root / "delivery").exists())

    def test_windows_cleanup_keeps_macos_and_unknown_build_siblings(self):
        windows = load_script("desktop/windows/build_windows.py")
        desktop = self.root / "desktop"
        for name in ("windows", "macos", "unowned"):
            folder = desktop / "build" / name
            folder.mkdir(parents=True)
            (folder / "keep.txt").write_bytes(name.encode())
        with mock.patch.object(windows, "DESKTOP", desktop), mock.patch.object(
            windows, "BUILD", desktop / "build/windows"
        ):
            windows.clean_build()
        self.assertFalse((desktop / "build/windows").exists())
        self.assertTrue((desktop / "build/macos/keep.txt").is_file(),
                        "Windows cleanup removed the macOS build")
        self.assertEqual((desktop / "build/macos/keep.txt").read_bytes(), b"macos")
        self.assertEqual((desktop / "build/unowned/keep.txt").read_bytes(), b"unowned")

    def test_windows_zip_rejects_oversize_before_touching_existing_archive(self):
        windows = load_script("desktop/windows/build_windows.py")
        payload = self.root / "payload"
        self.sparse(payload / "runtime/large.bin")
        archive = self.root / "payload.zip"
        archive.write_bytes(b"previous")
        original_read = Path.read_bytes

        def refuse_large_read(path):
            if path.stat().st_size > 314572800:
                raise AssertionError("oversize content read attempted")
            return original_read(path)

        with mock.patch.object(Path, "read_bytes", refuse_large_read):
            with self.assertRaisesRegex(ValueError, "300 MiB"):
                windows.zip_payload(payload, archive)
        self.assertEqual(archive.read_bytes(), b"previous")

    def test_windows_zip_rejects_business_data_without_packaging(self):
        windows = load_script("desktop/windows/build_windows.py")
        payload = self.root / "payload"
        payload.mkdir()
        (payload / "orders.xlsx").write_bytes(b"private")
        with self.assertRaises(ValueError):
            windows.zip_payload(payload, self.root / "payload.zip")
        self.assertFalse((self.root / "payload.zip").exists())

    def test_archive_case_collisions_are_rejected_before_extraction(self):
        # The ZIP name check must also protect Windows when built on a case-sensitive host.
        archive = self.root / "runtime.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("Version.json", b"one")
            output.writestr("version.json", b"two")
        shared = self.shared()
        with self.assertRaises(ValueError):
            shared.extract_runtime_zip(archive, self.root / "extracted")
        self.assertFalse((self.root / "extracted").exists())

    def test_macos_cleanup_can_remove_its_own_dmg_link_without_following_it(self):
        shared = self.shared()
        external = self.root / "Applications"
        external.mkdir()
        (external / "user-file").write_bytes(b"keep")
        build = self.root / "build/macos"
        image = build / "DMG内容"
        image.mkdir(parents=True)
        link = image / "Applications"
        if sys.platform == "win32":
            result = subprocess.run([
                "powershell.exe", "-NoProfile", "-Command",
                "New-Item -ItemType Junction -Path '" + str(link).replace("'", "''")
                + "' -Target '" + str(external).replace("'", "''") + "' | Out-Null",
            ], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        else:
            link.symlink_to(external, target_is_directory=True)
        shared.remove_build_tree(image, build)
        self.assertFalse(image.exists())
        self.assertEqual((external / "user-file").read_bytes(), b"keep")

    def test_build_entries_copy_identical_shared_sources_without_building(self):
        mac = load_script("desktop/build_macos.py")
        windows = load_script("desktop/windows/build_windows.py")
        desktop = self.repo / "desktop"
        (desktop / "macos").mkdir(parents=True)
        (desktop / "macos/Info.plist").write_bytes(
            b'<?xml version="1.0"?><plist version="1.0"><dict/></plist>')
        (desktop / "使用说明.html").write_bytes(b"mac manual")
        win = desktop / "windows"
        (win / "uninstall").mkdir(parents=True)
        (win / "使用说明.html").write_bytes(b"windows manual")
        (win / "uninstall/uninstall.ps1").write_bytes(b"# fixture")
        native = self.root / "native"
        native.mkdir()
        (native / "零售通.exe").write_bytes(b"not executable")
        mac_build = desktop / "build/macos"
        win_build = desktop / "build/windows"
        release = {**self.release, "features": ["dashboard"]}
        with mock.patch.multiple(mac, DEV=self.repo, ROOT=self.repo, DESKTOP=desktop,
                                 BUILD=mac_build, APP=mac_build / "零售通.app", RELEASE=release), \
                mock.patch.object(mac, "run"), mock.patch.object(mac, "runtime"), \
                mock.patch.object(mac, "icon"):
            app = mac.build(self.root / "unused-runtime")
        with mock.patch.multiple(windows, DEV=self.repo, ROOT=self.repo, DESKTOP=desktop,
                                 WINDOWS=win, BUILD=win_build, RELEASE=release), \
                mock.patch.object(windows, "prepare_runtime"):
            payload = windows.build_payload(release["windowsVersion"], native, None, None)
        mac_app = app / "Contents/Resources/app"
        self.assertEqual((mac_app / "shared-source-manifest.json").read_bytes(),
                         (payload / "app/shared-source-manifest.json").read_bytes())
        self.assertEqual(json.loads((mac_app / "版本.json").read_bytes())["platform"], "mac")
        self.assertEqual(json.loads((payload / "app/版本.json").read_bytes())["platform"], "windows")
        self.assertEqual((mac_app / "server.py").read_bytes(), b"print('server')\r\n")

    def test_macos_and_windows_build_directories_do_not_overlap(self):
        mac = load_script("desktop/build_macos.py")
        windows = load_script("desktop/windows/build_windows.py")
        self.assertEqual(mac.ROOT, ROOT)
        self.assertEqual(mac.BUILD, ROOT / "desktop/build/macos")
        self.assertEqual(windows.BUILD, ROOT / "desktop/build/windows")


if __name__ == "__main__":
    unittest.main(verbosity=2)
