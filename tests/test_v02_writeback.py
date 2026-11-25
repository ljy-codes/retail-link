"""Limited source patching must protect originals across preview, commit and restore."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT
TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DEV))
from backend.source_writeback import SourceWriteback, sha


class SourceWritebackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_DIR, prefix="writeback-")
        self.folder = Path(self.temp.name)
        self.path = self.folder / "中文 空格原件.xlsx"
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "月计划"
        sheet.append(["商品ID", "规格", "计划", "备注"])
        sheet.append(["001", "1件", 100, "保持"])
        sheet.append(["001", "1组", 200, "=C2+C3"])
        book.save(self.path)
        book.close()
        self.before = self.path.read_bytes()
        self.store = SourceWriteback(self.folder / "runtime")
        self.store.settings(True)
        self.entry = {"path": str(self.path), "sha256": sha(self.path)}
        self.template = self.store.register_template(self.entry, {"sheet": "月计划",
            "keyColumns": {"product": "A", "specification": "B"}, "fields": {"plan": "C"}})["template"]

    def tearDown(self):
        self.temp.cleanup()

    def preview(self, value=0):
        return self.store.preview(self.entry, {"templateId": self.template["id"],
            "changes": [{"keys": {"product": "001", "specification": "1件"}, "fields": {"plan": value}}]})

    def test_default_disabled_explicit_confirmation_and_zero(self):
        self.assertFalse(SourceWriteback(self.folder / "fresh").settings()["enabled"])
        preview = self.preview()
        self.assertEqual(self.path.read_bytes(), self.before)
        with self.assertRaises(ValueError):
            self.store.commit(preview["token"])
        result = self.store.commit(preview["token"], True)
        self.assertEqual(result["status"], "success")
        book = openpyxl.load_workbook(self.path)
        self.assertEqual(book["月计划"]["C2"].value, 0)
        self.assertEqual(book["月计划"]["D3"].value, "=C2+C3")
        self.assertEqual(book["月计划"]["A2"].value, "001")
        book.close()
        self.store.restore(result["id"], True)
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_preview_is_invalidated_by_external_change(self):
        preview = self.preview()
        book = openpyxl.load_workbook(self.path)
        book["月计划"]["D2"] = "外部变化"
        book.save(self.path)
        book.close()
        changed = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "变化"):
            self.store.commit(preview["token"], True)
        self.assertEqual(self.path.read_bytes(), changed)

    def test_restore_refuses_external_change_or_modified_backup(self):
        result = self.store.commit(self.preview()["token"], True)
        Path(result["backup"]).write_bytes(b"changed")
        changed = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.store.restore(result["id"], True)
        self.assertEqual(self.path.read_bytes(), changed)

    def test_ambiguous_product_requires_specification(self):
        with self.assertRaisesRegex(ValueError, "重复"):
            self.store.register_template(self.entry, {"sheet": "月计划", "productColumn": "A", "fields": {"plan": "C"}})

    def test_formula_field_and_undeclared_field_rejected(self):
        with self.assertRaises(ValueError):
            self.store.preview(self.entry, {"templateId": self.template["id"], "changes": [{"keys": {"product": "001", "specification": "1组"}, "fields": {"stockSmall": 5}}]})
        template = self.store.register_template(self.entry, {"sheet": "月计划", "keyColumns": {"product": "A", "specification": "B"}, "fields": {"plan": "D"}})["template"]
        with self.assertRaisesRegex(ValueError, "公式"):
            self.store.preview(self.entry, {"templateId": template["id"], "changes": [{"keys": {"product": "001", "specification": "1组"}, "fields": {"plan": 5}}]})
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_excel_lock_and_source_hardlink(self):
        lock = self.path.with_name("~$" + self.path.name)
        lock.write_bytes(b"locked")
        with self.assertRaisesRegex(ValueError, "Excel"):
            self.preview()
        lock.unlink()
        alias = self.folder / "别名.xlsx"
        alias.hardlink_to(self.path)
        with self.assertRaisesRegex(ValueError, "硬链接"):
            self.preview()

    def test_backup_failure_preserves_original(self):
        token = self.preview()["token"]
        with patch.object(self.store, "_copy", side_effect=OSError("backup failed")):
            with self.assertRaises(OSError):
                self.store.commit(token, True)
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_clear_value_and_disabled_setting_invalidates_preview(self):
        preview = self.preview(None)
        self.store.settings(False)
        with self.assertRaises(ValueError):
            self.store.commit(preview["token"], True)
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_pending_post_publish_record_can_restore_after_restart(self):
        result = self.store.commit(self.preview()["token"], True)
        meta = self.store.directory / "operations" / (result["id"] + ".json")
        saved = json.loads(meta.read_text(encoding="utf-8"))
        saved["status"] = "prepared"
        meta.write_text(json.dumps(saved), encoding="utf-8")
        restarted = SourceWriteback(self.folder / "runtime")
        restarted.restore(result["id"], True)
        self.assertEqual(self.path.read_bytes(), self.before)


if __name__ == "__main__":
    unittest.main()
