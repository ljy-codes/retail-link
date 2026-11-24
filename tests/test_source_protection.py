"""Fictional source protection, restored history and export race regressions.

All workbooks and runtime files are disposable; customer data is never read.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

TEST_DIR = Path(__file__).resolve().parent
ROOT = TEST_DIR.parent
DEV = ROOT / "开发空间" if (ROOT / "开发空间").is_dir() else ROOT
sys.path.insert(0, str(DEV))

import openpyxl
from tests.temp_support import temporary_directory
from backend.export_service import ExportError, ExportService


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def workbook(path, marker="虚构原始数据"):
    book = openpyxl.Workbook()
    book.active.append(["商品标识", "数量", "说明"])
    book.active.append(["SKU-001", 3, marker])
    book.save(path)
    book.close()
    return path


def report():
    return {"period": "2026-11", "asOf": "2026-11-20", "config": {"planBasis": "orders_amount"},
            "summary": {}, "products": [{"id": "SKU-001", "name": "虚构商品", "plan": 100,
                                          "completed": 20}], "issues": [], "sources": [], "trend": []}


class SourceServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory(prefix="source-protection-", directory=TEST_DIR)
        self.folder = Path(self.temp.name)
        self.output = self.folder / "outputs"
        self.output.mkdir()
        self.service = ExportService(self.folder / "runtime")
        self.sources = [workbook(self.folder / f"input-{n}.xlsx", str(n)) for n in range(2)]
        self.result = report()
        self.result["sourceInfo"] = {"path": str(self.folder / "canonical.xlsx")}
        self.result["importManifest"] = {"inputFiles": [{"path": str(p)} for p in self.sources]}

    def tearDown(self):
        self.temp.cleanup()

    def test_direct_service_protects_every_manifest_input_and_hardlink(self):
        before = {p: sha(p) for p in self.sources}
        for source in self.sources:
            with self.subTest(source=source.name), self.assertRaises(ExportError):
                self.service.create_xlsx(self.result, source.parent, source.name, overwrite=True)
            alias = self.output / source.name
            os.link(source, alias)
            with self.subTest(alias=alias.name), self.assertRaises(ExportError):
                self.service.create_xlsx(self.result, alias.parent, alias.name, overwrite=True)
            self.assertTrue(os.path.samefile(source, alias))
        self.assertEqual({p: sha(p) for p in self.sources}, before)

    def test_missing_original_path_remains_protected_but_new_export_succeeds(self):
        source = self.sources[0]
        source.unlink()
        with self.assertRaises(ExportError):
            self.service.create_xlsx(self.result, source.parent, source.name)
        self.assertFalse(source.exists())
        exported = self.service.create_xlsx(self.result, self.output, "new-report.xlsx")
        self.assertEqual(sha(exported["path"]), exported["sha256"])

    def test_restored_dataset_and_top_level_inputs_are_protected(self):
        for field in ("dataset", "top-level"):
            payload = report()
            inputs = {"inputFiles": [{"path": str(self.sources[-1])}]}
            payload.update(inputs if field == "top-level" else {field: inputs})
            with self.subTest(field=field), self.assertRaises(ExportError):
                self.service.create_xlsx(payload, self.sources[-1].parent, self.sources[-1].name, overwrite=True)

    def test_expected_preview_identity_prevents_same_byte_replacement_at_service_entry(self):
        target = workbook(self.output / "existing.xlsx")
        state = self.service.target_state(target)
        replacement = self.output / "replacement.xlsx"
        replacement.write_bytes(target.read_bytes())
        replacement.replace(target)
        with self.assertRaisesRegex(ExportError, "预览后发生变化"):
            self.service.create_xlsx(self.result, self.output, target.name, overwrite=True, expected_target=state)
        self.assertEqual(sha(target), state["sha256"])

    def test_source_hardlink_introduced_during_generation_is_preserved(self):
        source = self.sources[0]
        target = self.output / "existing.xlsx"
        target.write_bytes(source.read_bytes())
        before = sha(source)
        verify = self.service._verify_workbook
        def replace_with_source(path, expected=None):
            verified = verify(path, expected)
            if Path(path).name.startswith(".retail-export-"):
                target.unlink()
                os.link(source, target)
            return verified
        with patch.object(self.service, "_verify_workbook", side_effect=replace_with_source):
            with self.assertRaises(ExportError):
                self.service.create_xlsx(self.result, self.output, target.name, overwrite=True)
        self.assertTrue(os.path.samefile(source, target))
        self.assertEqual(sha(source), before)

    def test_same_byte_replacement_during_generation_requires_new_confirmation(self):
        target = workbook(self.output / "existing.xlsx")
        before = target.read_bytes()
        verify = self.service._verify_workbook
        def swap_same_bytes(path, expected=None):
            verified = verify(path, expected)
            if Path(path).name.startswith(".retail-export-"):
                replacement = self.output / "replacement.xlsx"
                replacement.write_bytes(before)
                replacement.replace(target)
            return verified
        with patch.object(self.service, "_verify_workbook", side_effect=swap_same_bytes):
            with self.assertRaisesRegex(ExportError, "生成期间目标文件已变化"):
                self.service.create_xlsx(self.result, self.output, target.name, overwrite=True)
        self.assertEqual(target.read_bytes(), before)

    def test_post_publish_external_change_is_not_overwritten_by_recovery(self):
        target = workbook(self.output / "existing.xlsx")
        external = b"fictional external writer after publish"
        verify = self.service._verify_workbook
        def change_after_publish(path, expected=None):
            if Path(path) == target:
                target.write_bytes(external)
                raise ValueError("injected external writer")
            return verify(path, expected)
        with patch.object(self.service, "_verify_workbook", side_effect=change_after_publish):
            with self.assertRaises(ExportError):
                self.service.create_xlsx(self.result, self.output, target.name, overwrite=True)
        self.assertEqual(target.read_bytes(), external)
        self.assertTrue(list(self.service.backups_dir.iterdir()))

    def test_post_publish_same_byte_replacement_is_preserved(self):
        target = workbook(self.output / "existing.xlsx")
        verify = self.service._verify_workbook
        replacement_identity = []
        def swap_after_publish(path, expected=None):
            verified = verify(path, expected)
            if Path(path) == target:
                replacement = self.output / "external.xlsx"
                replacement.write_bytes(target.read_bytes())
                replacement.replace(target)
                replacement_identity.append(target.stat().st_ino)
            return verified
        with patch.object(self.service, "_verify_workbook", side_effect=swap_after_publish):
            with self.assertRaises(ExportError):
                self.service.create_xlsx(self.result, self.output, target.name, overwrite=True)
        self.assertEqual(target.stat().st_ino, replacement_identity[0])
        self.assertTrue(list(self.service.backups_dir.iterdir()))

    def test_directory_replacement_during_generation_is_preserved(self):
        target = workbook(self.output / "existing.xlsx")
        before = target.read_bytes()
        moved = self.folder / "moved-outputs"
        external = b"fictional replacement directory content"
        verify = self.service._verify_workbook
        def replace_directory(path, expected=None):
            verified = verify(path, expected)
            if Path(path).name.startswith(".retail-export-"):
                self.output.rename(moved)
                self.output.mkdir()
                target.write_bytes(external)
            return verified
        with patch.object(self.service, "_verify_workbook", side_effect=replace_directory):
            with self.assertRaisesRegex(ExportError, "目标目录已变化"):
                self.service.create_xlsx(self.result, self.output, target.name, overwrite=True)
        self.assertEqual(target.read_bytes(), external)
        self.assertEqual((moved / target.name).read_bytes(), before)


class SourceHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("source_protection_fixture", TEST_DIR / "test_dataset_http.py")
        cls.fixture_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.fixture_module)

    def setUp(self):
        self.case = self.fixture_module.DatasetHttpTests()
        self.case.setUp()
        self.host = self.fixture_module.host
        external = self.case.folder / "external-user-input"
        external.mkdir()
        def make_external(filename, sheets):
            path = external / filename
            book = openpyxl.Workbook()
            book.remove(book.active)
            for title, rows in sheets.items():
                sheet = book.create_sheet(title)
                for row in rows:
                    sheet.append(row)
            book.save(path)
            book.close()
            self.case.paths.append(path)
            return self.case.success("/api/open", {"path": str(path)})["workbookId"]
        self.case.workbook = make_external
        _, _, _, reviewed = self.case.fixture()
        self.result = self.case.analyze(self.case.confirm(reviewed))
        self.before = {p: sha(p) for p in self.case.paths}

    def tearDown(self):
        try:
            self.assertEqual({p: sha(p) for p in self.case.paths}, self.before)
        finally:
            self.case.tearDown()

    def preview(self, target, snapshot=None):
        return self.case.call("/api/export/preview", {"snapshotId": (snapshot or self.result)["id"],
            "targetDir": str(target.parent), "filename": target.name})

    def forget_session(self, restore=False):
        for name in ("WORKBOOKS", "EXPORT_PREVIEWS", "DATASET_SERVICES", "HISTORY_STORES", "EXPORT_SERVICES"):
            getattr(self.host, name).clear()
        if restore:
            restored = self.case.success("/api/history/" + self.result["id"])
            self.assertTrue(restored["sourceAvailable"])
            self.assertEqual(len(self.host.WORKBOOKS), 1)

    def test_history_snapshot_protects_all_inputs_without_restoring_a_session(self):
        self.forget_session()
        for source in self.case.paths:
            self.assertEqual(self.preview(source)[0], 400)
        target = self.case.output / "safe-report.xlsx"
        status, preview = self.preview(target)
        self.assertEqual(status, 200, preview)
        self.assertEqual(self.case.call("/api/export/xlsx", {"token": preview["token"]})[0], 200)

    def test_restored_composite_protects_originals_and_their_hardlinks(self):
        self.forget_session(restore=True)
        for index, source in enumerate(self.case.paths):
            self.assertEqual(self.preview(source)[0], 400)
            alias = self.case.output / f"alias-{index}.xlsx"
            os.link(source, alias)
            self.assertEqual(self.preview(alias)[0], 400)
            self.assertTrue(os.path.samefile(source, alias))

    def test_restored_dataset_protects_sources_when_exporting_an_unrelated_snapshot(self):
        self.forget_session(restore=True)
        unrelated = self.host.history_store().save(report())
        self.assertEqual(self.preview(self.case.paths[-1], unrelated)[0], 400)

    def test_commit_rechecks_sources_registered_after_preview(self):
        target = workbook(self.case.output / "newly-imported.xlsx")
        status, preview = self.preview(target)
        self.assertEqual(status, 200, preview)
        before = sha(target)
        self.case.success("/api/open", {"path": str(target)})
        status, payload = self.case.call("/api/export/xlsx", {"token": preview["token"], "overwrite": True})
        self.assertEqual(status, 400, payload)
        self.assertEqual(sha(target), before)

    def test_commit_rejects_same_byte_inode_swap_after_preview(self):
        target = workbook(self.case.output / "existing.xlsx")
        status, preview = self.preview(target)
        self.assertEqual(status, 200, preview)
        before = target.read_bytes()
        replacement = self.case.output / "replacement.xlsx"
        replacement.write_bytes(before)
        replacement.replace(target)
        status, payload = self.case.call("/api/export/xlsx", {"token": preview["token"], "overwrite": True})
        self.assertEqual(status, 400, payload)
        self.assertEqual(target.read_bytes(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
