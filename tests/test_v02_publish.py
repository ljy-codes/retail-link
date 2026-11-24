"""Atomic no-clobber publication with hard links unavailable. Synthetic files only."""
from contextlib import closing
import errno
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT
sys.path.insert(0, str(DEV))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backend import file_publish
from backend.export_service import ExportService, ExportError
from backend.history_store import HistoryStore
from backend.daily_records import DailyStore
from backend.migration_service import MigrationService
from test_export_service import sample_result


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def no_links(*args, **kwargs):
    raise OSError(errno.ENOTSUP, "synthetic filesystem without hard links")


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="v02-publish-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "完成的原始副本.dat"
        self.source.write_bytes(b"complete-source\x00" * 100000)
        self.before = digest(self.source)
        self.target = self.root / "目标文件.dat"

    def assert_clean(self):
        self.assertEqual(list(self.root.glob(".publish-*")), [])

    def test_hardlink_path_retains_source_and_returns_known_published_identity(self):
        identity = file_publish.publish_no_clobber(self.source, self.target)
        self.assertEqual(identity, (self.target.stat().st_dev, self.target.stat().st_ino))
        self.assertEqual(identity, (self.source.stat().st_dev, self.source.stat().st_ino))
        self.assertEqual(digest(self.source), self.before)
        self.assertEqual(digest(self.target), self.before)

    def test_enotsup_copies_flushes_then_native_rename_retains_source(self):
        rename = file_publish._rename_exclusive
        identities = []
        def exclusive(stage, target):
            self.assertFalse(Path(target).exists())
            self.assertEqual(digest(stage), self.before)
            self.assertEqual(digest(self.source), self.before)
            info = Path(stage).stat()
            identities.append((info.st_dev, info.st_ino))
            rename(stage, target)
        with patch.object(file_publish.os, "link", side_effect=no_links), patch.object(file_publish, "_rename_exclusive", side_effect=exclusive):
            identity = file_publish.publish_no_clobber(self.source, self.target)
        self.assertEqual(identity, identities[0])
        self.assertEqual(identity, (self.target.stat().st_dev, self.target.stat().st_ino))
        self.assertNotEqual(identity, (self.source.stat().st_dev, self.source.stat().st_ino))
        self.assertEqual(digest(self.target), self.before)
        self.assertEqual(digest(self.source), self.before)
        self.assert_clean()

    def test_existing_target_is_never_overwritten_when_links_are_unavailable(self):
        self.target.write_bytes(b"existing unrelated file")
        with patch.object(file_publish.os, "link", side_effect=no_links):
            with self.assertRaises(FileExistsError):
                file_publish.publish_no_clobber(self.source, self.target)
        self.assertEqual(self.target.read_bytes(), b"existing unrelated file")
        self.assertEqual(digest(self.source), self.before)
        self.assert_clean()

    def test_target_appearing_after_copy_is_preserved(self):
        rename = file_publish._rename_exclusive
        def concurrent_writer(stage, target):
            self.assertFalse(Path(target).exists())
            self.assertEqual(digest(stage), self.before)
            Path(target).write_bytes(b"external writer won")
            rename(stage, target)
        with patch.object(file_publish.os, "link", side_effect=no_links), patch.object(file_publish, "_rename_exclusive", side_effect=concurrent_writer):
            with self.assertRaises(FileExistsError):
                file_publish.publish_no_clobber(self.source, self.target)
        self.assertEqual(self.target.read_bytes(), b"external writer won")
        self.assertEqual(digest(self.source), self.before)
        self.assert_clean()

    def test_failed_copy_never_exposes_partial_target_or_removes_source(self):
        def partial_copy(source, output, **kwargs):
            self.assertFalse(self.target.exists())
            output.write(b"partial")
            output.flush()
            self.assertFalse(self.target.exists())
            raise OSError(errno.ENOSPC, "synthetic disk full")
        with patch.object(file_publish.os, "link", side_effect=no_links), patch.object(file_publish.shutil, "copyfileobj", side_effect=partial_copy):
            with self.assertRaises(OSError):
                file_publish.publish_no_clobber(self.source, self.target)
        self.assertFalse(self.target.exists())
        self.assertEqual(digest(self.source), self.before)
        self.assert_clean()

    def test_dangling_target_symlink_is_occupied(self):
        self.target.symlink_to(self.root / "does-not-exist")
        with patch.object(file_publish.os, "link", side_effect=no_links):
            with self.assertRaises(FileExistsError):
                file_publish.publish_no_clobber(self.source, self.target)
        self.assertTrue(self.target.is_symlink())
        self.assertFalse((self.root / "does-not-exist").exists())
        self.assertEqual(digest(self.source), self.before)
        self.assert_clean()

    def test_absent_exclusive_primitive_fails_closed(self):
        with patch.object(file_publish.os, "link", side_effect=no_links), patch.object(file_publish, "_rename_exclusive", side_effect=OSError(errno.ENOTSUP, "no exclusive rename")):
            with self.assertRaises(OSError):
                file_publish.publish_no_clobber(self.source, self.target)
        self.assertFalse(self.target.exists())
        self.assertEqual(digest(self.source), self.before)
        self.assert_clean()

    def test_real_native_exclusive_rename_refuses_existing_destination(self):
        stage = self.root / "native-stage"
        stage.write_bytes(b"native complete data")
        file_publish._rename_exclusive(stage, self.target)
        self.assertFalse(stage.exists())
        self.assertEqual(self.target.read_bytes(), b"native complete data")
        stage.write_bytes(b"a later complete file")
        with self.assertRaises(FileExistsError):
            file_publish._rename_exclusive(stage, self.target)
        self.assertEqual(stage.read_bytes(), b"a later complete file")
        self.assertEqual(self.target.read_bytes(), b"native complete data")

    def test_permission_error_does_not_try_an_unrelated_fallback(self):
        with patch.object(file_publish.os, "link", side_effect=PermissionError(errno.EACCES, "denied")), patch.object(file_publish, "_rename_exclusive") as rename:
            with self.assertRaises(PermissionError):
                file_publish.publish_no_clobber(self.source, self.target)
            rename.assert_not_called()
        self.assertFalse(self.target.exists())
        self.assertEqual(digest(self.source), self.before)

    def test_generated_workbook_uses_the_fallback_stage_identity(self):
        service = ExportService(self.root / "data")
        with patch.object(file_publish.os, "link", side_effect=no_links):
            info = service.create_xlsx(sample_result(), self.root, "完整导出.xlsx")
        target = Path(info["path"])
        self.assertEqual(info["sha256"], digest(target))
        with closing(openpyxl.load_workbook(target, read_only=True)) as book:
            self.assertEqual(book.sheetnames, list(service.SHEETS))
        self.assertEqual(list(self.root.glob(".retail-export-*")), [])
        self.assert_clean()

    def test_log_export_keeps_original_and_hash_when_links_are_unavailable(self):
        service = ExportService(self.root / "data")
        service.log_operation("synthetic", {"result": "success"})
        before = service.log_path.read_bytes()
        with patch.object(file_publish.os, "link", side_effect=no_links):
            info = service.export_logs(self.root / "导出日志.jsonl")
        self.assertEqual(service.log_path.read_bytes(), before)
        self.assertEqual(Path(info["path"]).read_bytes(), before)
        self.assertEqual(info["sha256"], digest(info["path"]))
        self.assert_clean()

    def test_history_save_delete_restore_work_without_hardlinks(self):
        history = HistoryStore(self.root / "data")
        with patch.object(file_publish.os, "link", side_effect=no_links):
            saved = history.save({"id": "123", "products": [{"id": "A"}], "summary": {}})
            active = history.history_dir / "123.json"
            before = active.read_bytes()
            history.delete("123")
            self.assertFalse(active.exists())
            self.assertEqual((history.deleted_dir / "123.json").read_bytes(), before)
            history.restore("123")
            self.assertEqual(active.read_bytes(), before)
            self.assertEqual(history.get("123"), saved)
        self.assertEqual(list(history.data_dir.rglob(".publish-*")), [])

    def test_migration_zip_is_complete_and_existing_database_is_retained(self):
        data = self.root / "data"
        daily = DailyStore(data)
        daily.save({"date": "2025-11-01", "product": "A", "price": 1})
        before = digest(daily.path)
        migration = MigrationService(data)
        with patch.object(file_publish.os, "link", side_effect=no_links):
            result = migration.export_bundle(self.root / "迁移.zip")
        self.assertEqual(digest(daily.path), before)
        self.assertEqual(result["sha256"], digest(result["path"]))
        with zipfile.ZipFile(result["path"]) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            for item in manifest["files"]:
                self.assertEqual(hashlib.sha256(archive.read(item["path"])).hexdigest(), item["sha256"])
        self.assert_clean()

    def test_external_same_byte_replacement_after_export_is_not_rolled_back(self):
        service = ExportService(self.root / "data")
        target = self.root / "external-replacement.xlsx"
        verify = service._verify_workbook
        external_id = []
        def replace_after_publish(path, expected=None):
            result = verify(path, expected)
            if Path(path) == target:
                replacement = self.root / "external.xlsx"
                replacement.write_bytes(target.read_bytes())
                replacement.replace(target)
                external_id.append(target.stat().st_ino)
            return result
        with patch.object(file_publish.os, "link", side_effect=no_links), patch.object(service, "_verify_workbook", side_effect=replace_after_publish):
            with self.assertRaises(ExportError):
                service.create_xlsx(sample_result(), self.root, target.name)
        self.assertTrue(target.is_file())
        self.assertEqual(target.stat().st_ino, external_id[0])
        self.assert_clean()


if __name__ == "__main__":
    unittest.main(verbosity=2)
