"""Behavior tests for the local Excel output, rollback, logs, and migration bundle."""
from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
import threading
import zipfile

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT
sys.path.insert(0, str(DEV))


def sample_result(completed=1234.0):
    return {
        "id": "analysis-test-1",
        "filename": "fixture.xlsx",
        "asOf": "2026-11-22",
        "period": "2026-11",
        "appVersion": "0.1.1",
        "config": {"planBasis": "orders_amount", "targetRate": 0.85},
        "summary": {
            "plan": 10000.0,
            "completed": completed,
            "achievement": completed / 10000.0,
            "targetRate": 0.85,
            "targetGap": max(0, 8500 - completed),
            "stockAmount": 500.0,
            "transitAmount": None,
            "outboundAmount": 1200.0,
            "supplyAmount": 1000.0,
            "discountRate": 1000 / 1200,
            "markdownRate": 1 - 1000 / 1200,
            "lossAmount": None,
            "lossRate": None,
            "productCount": 2,
            "salesQuantity": 80,
            "issueCount": 1,
        },
        "products": [
            {
                "id": "SKU-001", "name": "测试商品甲", "code": "001",
                "pack": 6, "warehouse": "南京仓", "plan": 6000,
                "completed": completed, "achievement": completed / 6000,
                "targetGap": max(0, 5100 - completed), "dailyRequired": None,
                "stockLarge": 10, "stockSmall": 60, "stockAmount": 500,
                "transitSmall": None, "transitAmount": None,
                "yesterdaySales": 8, "averageSales": None, "coverageDays": None,
                "outboundQuantity": 50, "outboundAmount": 1200,
                "supplyAmount": 1000, "discountRate": 1000 / 1200,
                "markdownRate": 1 - 1000 / 1200, "lossAmount": None,
                "lossRate": None, "status": "warning", "issues": ["missing loss"],
                "sources": {"completed": "订单明细!A2", "stockAmount": "库存表!I2"},
            },
            {
                "id": "SKU-002", "name": "测试商品乙", "code": "002",
                "pack": 1, "warehouse": "芜湖仓", "plan": None,
                "completed": None, "achievement": None,
                "targetGap": None, "dailyRequired": None,
                "stockLarge": None, "stockSmall": None, "stockAmount": None,
                "transitSmall": None, "transitAmount": None,
                "yesterdaySales": None, "averageSales": None, "coverageDays": None,
                "outboundQuantity": 0, "outboundAmount": None,
                "supplyAmount": None, "discountRate": None,
                "markdownRate": None, "lossAmount": None,
                "lossRate": None, "status": "info", "issues": [], "sources": {},
            },
        ],
        "issues": [{"id": "missing-loss", "severity": "warning", "code": "MISSING_LOSS",
                    "title": "货损数据缺失", "detail": "请补充货损记录。", "sheet": "货损", "cell": "A2", "product": "SKU-001"}],
        "trend": [{"date": "2026-11-21", "sales": 80, "orders": 10, "outbound": 50}],
        "sources": [{"role": "inventory", "label": "库存", "sheet": "库存表", "records": 2,
                      "startDate": None, "endDate": None}],
    }


class ExportServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="export-service-", dir=ROOT / "tests")
        self.data_dir = Path(self.temp.name) / "data"
        self.output_dir = Path(self.temp.name) / "output"
        self.output_dir.mkdir(parents=True)
        self.module = importlib.import_module("backend.export_service")
        self.service = self.module.ExportService(self.data_dir, "0.1.1")

    def tearDown(self):
        self.temp.cleanup()

    def test_generates_multi_sheet_xlsx_and_verifies_content(self):
        result = self.service.create_xlsx(sample_result(), self.output_dir, "看盘结果.xlsx")
        path = Path(result["path"])
        self.assertTrue(path.is_file())
        self.assertTrue(path.name.endswith(".xlsx"))
        self.assertEqual(result["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(result.get("sheets"), ["看盘总览", "商品明细", "库存与销量", "数据问题", "数据来源"])
        self.assertEqual(list(self.output_dir.glob("*.part")), [])
        workbook = load_workbook(path, data_only=True, read_only=True)
        cells = [cell.value for sheet in workbook.worksheets for row in sheet.iter_rows() for cell in row]
        self.assertIn("测试商品甲", cells)
        self.assertIn("货损数据缺失", cells)
        self.assertNotIn("—", cells)
        workbook.close()

    def test_same_target_is_protected_and_overwrite_creates_backup(self):
        first = self.service.create_xlsx(sample_result(1000), self.output_dir, "重复.xlsx")
        with self.assertRaises(Exception):
            self.service.create_xlsx(sample_result(1100), self.output_dir, "重复.xlsx")
        second = self.service.create_xlsx(sample_result(1100), self.output_dir, "重复.xlsx", overwrite=True)
        self.assertTrue(second.get("backupPath"), second)
        self.assertTrue(Path(second["backupPath"]).is_file(), second)
        self.assertNotEqual(first["sha256"], second["sha256"])
        self.assertEqual(list(self.output_dir.glob("*.part")), [])

    def test_rollback_restores_the_previous_file_and_logs_operation(self):
        first = self.service.create_xlsx(sample_result(1000), self.output_dir, "回滚.xlsx")
        second = self.service.create_xlsx(sample_result(2000), self.output_dir, "回滚.xlsx", overwrite=True)
        restored = self.service.rollback(second["operationId"])
        self.assertEqual(restored.get("result"), "rolled_back", restored)
        self.assertEqual(hashlib.sha256(Path(first["path"]).read_bytes()).hexdigest(), first["sha256"])
        logs = self.service.list_logs()
        if isinstance(logs, dict): logs = logs.get("items", logs.get("logs", []))
        self.assertGreaterEqual(len(logs), 3)
        serialized = json.dumps(logs, ensure_ascii=False)
        self.assertNotIn("测试商品甲", serialized)
        self.assertIn("rolled_back", serialized)


    def _values(self, sheet):
        return [cell.value for row in sheet for cell in row]

    def test_missing_is_blank_and_real_zero_remains_zero(self):
        output = self.service.create_xlsx(sample_result(), self.output_dir, "空缺与零.xlsx")
        with __import__('contextlib').closing(load_workbook(output["path"], data_only=True)) as wb:
            sheet = wb["商品明细"]
            labels = {c.value: c.column for c in sheet[1]}
            self.assertIsNone(sheet.cell(3, labels["月计划"]).value)
            self.assertEqual(sheet.cell(3, labels["出库小单位数量"]).value, 0)
            inventory = wb["库存与销量"]
            inventory_labels = {c.value: c.column for c in inventory[1]}
            self.assertIsNone(inventory.cell(3, inventory_labels["现货金额"]).value)

    def test_text_ids_keep_leading_zero_and_formula_like_names_stay_text(self):
        fixture = sample_result()
        fixture["products"][0].update({"id": "001234567890123456789", "code": "0000123", "name": "=1+2"})
        fixture["issues"][0]["detail"] = "+SUM(A1:A2)"
        output = self.service.create_xlsx(fixture, self.output_dir, "编码文本.xlsx")
        with __import__('contextlib').closing(load_workbook(output["path"], data_only=False)) as wb:
            detail = wb["商品明细"]
            labels = {c.value: c.column for c in detail[1]}
            ident = detail.cell(2, labels["商品标识"])
            code = detail.cell(2, labels["商品编码"])
            name = detail.cell(2, labels["商品名称"])
            self.assertEqual(ident.value, "001234567890123456789")
            self.assertEqual(code.value, "0000123")
            self.assertEqual(ident.data_type, "s")
            self.assertEqual(name.value, "=1+2")
            self.assertEqual(name.data_type, "s")
            formulas = [c.coordinate for ws in wb for row in ws for c in row if c.data_type == "f"]
            self.assertEqual(formulas, [])

    def test_ratios_have_percent_formats_but_loss_amount_is_currency(self):
        output = self.service.create_xlsx(sample_result(), self.output_dir, "数字格式.xlsx")
        with __import__('contextlib').closing(load_workbook(output["path"], data_only=False)) as wb:
            overview = wb["看盘总览"]
            metrics = {row[0].value: row[1] for row in overview.iter_rows(min_row=2) if row[0].value}
            for label in ("达成率", "目标门槛", "折扣比例", "货损率"):
                self.assertIn("%", metrics[label].number_format, label)
            self.assertNotIn("%", metrics["货损金额"].number_format)
            self.assertEqual(metrics["达成率"].value, 0.1234)

    def test_export_refuses_source_workbook_path_even_with_overwrite(self):
        source = self.output_dir / "来源.xlsx"
        original = self.service.create_xlsx(sample_result(), self.output_dir, source.name)
        fixture = sample_result(9999)
        fixture["sourceInfo"] = {"path": str(source), "sha256": original["sha256"]}
        with self.assertRaises(ValueError):
            self.service.create_xlsx(fixture, self.output_dir, source.name, overwrite=True)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original["sha256"])

    def test_invalid_target_and_filename_cannot_escape_requested_directory(self):
        for filename in ("../outside.xlsx", "sub/name.xlsx", "~$locked.xlsx"):
            with self.assertRaises(ValueError, msg=filename):
                self.service.create_xlsx(sample_result(), self.output_dir, filename)
        with self.assertRaises(ValueError):
            self.service.create_xlsx(sample_result(), self.output_dir / "不存在", "file.xlsx")
        self.assertFalse((self.output_dir.parent / "outside.xlsx").exists())

    def test_symlink_target_is_not_followed_or_replaced(self):
        original = self.service.create_xlsx(sample_result(), self.output_dir, "真实.xlsx")
        link = self.output_dir / "链接.xlsx"
        link.symlink_to(Path(original["path"]))
        with self.assertRaises(ValueError):
            self.service.create_xlsx(sample_result(9999), self.output_dir, link.name, overwrite=True)
        self.assertTrue(link.is_symlink())
        self.assertEqual(hashlib.sha256(Path(original["path"]).read_bytes()).hexdigest(), original["sha256"])

    def test_publish_validation_failure_restores_previous_bytes(self):
        old = self.service.create_xlsx(sample_result(1000), self.output_dir, "故障恢复.xlsx")
        output = Path(old["path"])
        verify = self.service._verify_workbook
        def fail_after_publish(path, expected=None):
            if Path(path) == output:
                raise ValueError("injected post-publish verification failure")
            return verify(path, expected)
        with patch.object(self.service, "_verify_workbook", side_effect=fail_after_publish):
            with self.assertRaises(Exception):
                self.service.create_xlsx(sample_result(2000), self.output_dir, output.name, overwrite=True)
        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), old["sha256"])
        self.assertEqual([p for p in self.output_dir.iterdir() if p.name.startswith('.')], [])
        self.assertTrue(any(x.get("result") in ("rolled_back", "failed") for x in self.service.list_logs()))

    def test_failed_first_export_leaves_no_invalid_result(self):
        with patch.object(self.service, "_verify_workbook", side_effect=ValueError("fixture verification failed")):
            with self.assertRaises(Exception):
                self.service.create_xlsx(sample_result(), self.output_dir, "失败新建.xlsx")
        self.assertFalse((self.output_dir / "失败新建.xlsx").exists())
        self.assertEqual([p for p in self.output_dir.iterdir() if p.name.startswith('.')], [])

    def test_backup_failure_never_changes_existing_file(self):
        old = self.service.create_xlsx(sample_result(1000), self.output_dir, "无法备份.xlsx")
        with patch.object(self.service, "_backup", side_effect=OSError("fixture disk error")):
            with self.assertRaises(Exception):
                self.service.create_xlsx(sample_result(2000), self.output_dir, "无法备份.xlsx", overwrite=True)
        self.assertEqual(hashlib.sha256(Path(old["path"]).read_bytes()).hexdigest(), old["sha256"])

    def test_partial_backup_is_cleaned_when_backup_copy_fails(self):
        old = self.service.create_xlsx(sample_result(1000), self.output_dir, "不完整备份.xlsx")
        before_backups = set(self.service.backups_dir.iterdir())
        def fail_copy(source, destination, *args, **kwargs):
            destination.write(b"partial backup")
            raise OSError("injected backup write failure")
        with patch.object(self.module.shutil, "copyfileobj", side_effect=fail_copy):
            with self.assertRaises(Exception):
                self.service.create_xlsx(sample_result(2000), self.output_dir, "不完整备份.xlsx", overwrite=True)
        self.assertEqual(hashlib.sha256(Path(old["path"]).read_bytes()).hexdigest(), old["sha256"])
        self.assertEqual(set(self.service.backups_dir.iterdir()), before_backups)

    def test_two_simultaneous_new_exports_do_not_clobber_each_other(self):
        barrier = threading.Barrier(2)
        def attempt(value):
            barrier.wait(timeout=5)
            try:
                return self.service.create_xlsx(sample_result(value), self.output_dir, "同名并发.xlsx")
            except self.module.AlreadyExistsError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, (1000, 2000)))
        successes = [item for item in outcomes if item]
        self.assertEqual(len(successes), 1)
        self.assertEqual(hashlib.sha256(Path(successes[0]["path"]).read_bytes()).hexdigest(), successes[0]["sha256"])
        self.assertEqual([p for p in self.output_dir.iterdir() if p.name.startswith('.')], [])

    def test_rollback_refuses_external_edits_and_preserves_them(self):
        self.service.create_xlsx(sample_result(1000), self.output_dir, "外部更改.xlsx")
        latest = self.service.create_xlsx(sample_result(2000), self.output_dir, "外部更改.xlsx", overwrite=True)
        output = Path(latest["path"])
        modified = output.read_bytes() + b"external change"
        output.write_bytes(modified)
        with self.assertRaises(ValueError):
            self.service.rollback(latest["operationId"])
        self.assertEqual(output.read_bytes(), modified)

    def test_corrupt_backup_cannot_be_restored(self):
        self.service.create_xlsx(sample_result(1000), self.output_dir, "备份损坏.xlsx")
        latest = self.service.create_xlsx(sample_result(2000), self.output_dir, "备份损坏.xlsx", overwrite=True)
        Path(latest["backupPath"]).write_bytes(b"corrupt backup")
        with self.assertRaises(ValueError):
            self.service.rollback(latest["operationId"])
        self.assertEqual(hashlib.sha256(Path(latest["path"]).read_bytes()).hexdigest(), latest["sha256"])

    def test_repeated_rollback_is_rejected_and_restore_itself_can_be_undone(self):
        first = self.service.create_xlsx(sample_result(1000), self.output_dir, "恢复再撤销.xlsx")
        second = self.service.create_xlsx(sample_result(2000), self.output_dir, "恢复再撤销.xlsx", overwrite=True)
        restored = self.service.rollback(second["operationId"])
        with self.assertRaises(ValueError):
            self.service.rollback(second["operationId"])
        undone = self.service.rollback(restored["operationId"])
        self.assertEqual(hashlib.sha256(Path(undone["path"]).read_bytes()).hexdigest(), second["sha256"])

    def test_audit_records_operation_metadata_without_product_payload(self):
        result = self.service.create_xlsx(sample_result(), self.output_dir, "审计.xlsx")
        rows = self.service.list_logs()
        self.assertTrue(any(x.get("operationId") == result["operationId"] for x in rows))
        for log in rows:
            for field in ("operation", "result", "time"):
                self.assertIn(field, log)
        serialized = json.dumps(rows, ensure_ascii=False)
        self.assertNotIn("测试商品甲", serialized)
        self.assertNotIn("请补充货损记录", serialized)


if __name__ == "__main__":
    unittest.main(verbosity=2)
