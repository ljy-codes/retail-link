"""Public HTTP contract for reviewed imports, persistence and Excel provenance.

Every writable fixture lives in a disposable directory inside 测试空间.
The workbooks contain only fictional products and no customer source data.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import openpyxl
from tests.temp_support import temporary_directory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("dataset_http_host", ROOT / "server.py")
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class DatasetHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory(prefix="dataset-http-", directory=Path(__file__).parent)
        self.folder = Path(self.temp.name)
        host.APP_DIR = self.folder / "app"
        host.PROJECT_DIR = host.APP_DIR
        host.DATA_DIR = self.folder / "runtime"
        host.ORIGINAL_DIR = self.folder / "originals"
        host.ORIGINAL_DIR.mkdir()
        host.SAMPLE = host.ORIGINAL_DIR / "unused-sample.xlsx"
        self.output = self.folder / "outputs"
        self.output.mkdir()
        for name in ("WORKBOOKS", "HISTORY_STORES", "EXPORT_SERVICES", "MIGRATION_SERVICES",
                     "EXPORT_PREVIEWS", "DATASET_SERVICES"):
            getattr(host, name).clear()
        self.server = host.ThreadingHTTPServer(("127.0.0.1", 0), host.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.paths = []

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def call(self, path, body=None):
        request = Request(self.url + path,
                          data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
                          headers={"Content-Type": "application/json"} if body is not None else {})
        try:
            with urlopen(request, timeout=30) as response:
                payload = response.read()
                return response.status, json.loads(payload) if "json" in response.headers.get("Content-Type", "") else payload
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def success(self, route, body=None):
        status, response = self.call(route, body)
        self.assertEqual(status, 200, response)
        return response

    def test_missing_sample_returns_actionable_response(self):
        status, response = self.call("/api/sample")
        self.assertEqual(status, 400)
        self.assertIn("未附带样例", response["error"])

    def test_unknown_api_returns_json_404(self):
        status, response = self.call("/api/not-found")
        self.assertEqual(status, 404)
        self.assertEqual(response["error"], "接口不存在。")

    def workbook(self, filename, sheets):
        path = host.ORIGINAL_DIR / filename
        workbook = openpyxl.Workbook()
        workbook.remove(workbook.active)
        for title, rows in sheets.items():
            sheet = workbook.create_sheet(title)
            for row in rows:
                sheet.append(row)
        workbook.save(path)
        workbook.close()
        self.paths.append(path)
        inspection = self.success("/api/open", {"path": str(path)})
        return inspection["workbookId"]

    def fixture(self, template_id=None):
        ids = [self.workbook("虚构库存.xlsx", {
            "南京库存": [["商品标识", "仓库", "箱规", "库存", "单价"], ["SKU-001", "南京", 6, 12, 10]],
            "芜湖库存": [["商品标识", "仓库", "箱规", "库存", "单价"], ["SKU-001", "芜湖", 6, 18, 10]],
            # Invalid quantities in an explicitly ignored sheet must never enter normalization.
            "不纳入说明": [["商品标识", "库存"], ["DO-NOT-COUNT", "非数字"]],
        }), self.workbook("虚构明细.xlsx", {
            "销售明细": [["商品标识", "日期", "数量", "单价", "供价"], ["SKU-001", "2026-11-19", 4, 10, 8]],
            "出库明细": [["商品标识", "日期", "数量", "单价", "供价"], ["SKU-001", "2026-11-19", 5, 10, 8]],
            "订单明细": [["商品标识", "日期", "数量", "单价", "供价"], ["SKU-001", "2026-11-19", 5, 60, 48]],
        }), self.workbook("虚构月计划.xlsx", {
            "月计划": [["商品标识", "计划金额"], ["SKU-001", 1000]],
        })]
        staged = self.success("/api/import/stage", {"workbookIds": ids, "templateId": template_id})
        selections = []
        for source in staged["sheets"]:
            choice = {"sourceId": source["sourceId"], "headerRow": 1, "role": "ignore", "columns": {}, "unit": "small"}
            title = source["sheet"]
            if title.endswith("库存"):
                choice.update(role="inventory", snapshotDate="2026-11-20", columns={"product": "A", "warehouse": "B", "pack": "C", "stockSmall": "D", "basePrice": "E"})
            elif title == "月计划":
                choice.update(role="summary", columns={"product": "A", "plan": "B"})
            elif title in {"销售明细", "出库明细", "订单明细"}:
                choice.update(role={"销售明细": "sales", "出库明细": "outbound", "订单明细": "orders"}[title],
                              columns={"product": "A", "date": "B", "quantity": "C", "basePrice": "D", "supplyPrice": "E"},
                              unit="large" if title == "订单明细" else "small")
            selections.append(choice)
        config = dict(staged["config"], asOf="2026-11-20", period="2026-11", averageDays=1, inventoryComparable=True)
        preview = self.success("/api/import/preview", {"sessionId": staged["sessionId"], "selections": selections, "config": config})
        self.assertTrue(preview["canContinue"], preview)
        self.assertEqual(preview["selectedCount"], 6)
        self.assertEqual(preview["excludedCount"], 1)
        return staged, selections, config, preview

    def confirm(self, preview, name="虚构测试导入格式"):
        response = self.success("/api/import/confirm", {"token": preview["token"], "confirmed": True, "templateName": name})
        return response["inspection"]

    def analyze(self, inspection):
        return self.success("/api/analyze", {"workbookId": inspection["workbookId"], "config": inspection["config"]})

    def templates(self):
        return self.success("/api/import/templates")["templates"]

    def history_count(self):
        return self.success("/api/history")["total"]

    def export_bytes(self, snapshot):
        preview = self.success("/api/export/preview", {"snapshotId": snapshot["id"], "targetDir": str(self.output), "filename": "虚构看盘.xlsx"})
        exported = self.success("/api/export/xlsx", {"token": preview["token"]})
        payload = self.success("/api/export/download/" + exported["operationId"])
        self.assertEqual(hashlib.sha256(payload).hexdigest(), exported["sha256"])
        return payload

    def test_review_then_confirmation_then_analysis_is_the_persistence_boundary(self):
        _, _, _, preview = self.fixture()
        before = {path: sha256(path) for path in self.paths}
        self.assertEqual(self.history_count(), 0)
        self.assertEqual(self.templates(), [])
        for confirmed in (False, None, "true"):
            status, body = self.call("/api/import/confirm", {"token": preview["token"], "confirmed": confirmed})
            self.assertEqual(status, 400, body)
        inspection = self.confirm(preview)
        self.assertEqual(self.history_count(), 0)
        self.assertEqual(self.templates(), [])
        result = self.analyze(inspection)
        self.assertEqual(self.history_count(), 1)
        self.assertEqual(len(self.templates()), 1)
        self.assertEqual(self.templates()[0]["name"], "虚构测试导入格式")
        self.assertEqual(len(result["products"]), 1)
        product = result["products"][0]
        self.assertEqual(product["stockSmall"], 30)
        self.assertEqual(product["salesQuantity"], 4)
        self.assertEqual(product["completed"], 300)
        self.assertAlmostEqual(product["discountRate"], .8)
        self.assertEqual(set(product["warehouse"].split("、")), {"南京", "芜湖"})
        manifest = result["importManifest"]
        self.assertEqual({item["filename"] for item in manifest["inputFiles"]}, {p.name for p in self.paths})
        self.assertEqual(len(manifest["selectionManifest"]), 7)
        self.assertTrue(manifest["provenance"])
        self.assertEqual({path: sha256(path) for path in self.paths}, before)

    def test_default_generated_folder_is_allowed_but_runtime_folders_are_protected(self):
        _, _, _, preview = self.fixture()
        result = self.analyze(self.confirm(preview))
        default = host.PROJECT_DIR / "生成文件"
        default.mkdir(parents=True)
        request = {"snapshotId": result["id"], "targetDir": str(default), "filename": "默认输出.xlsx"}
        self.assertEqual(self.call("/api/export/preview", request)[0], 200)
        protected = host.APP_DIR / "backend"
        protected.mkdir()
        request["targetDir"] = str(protected)
        self.assertEqual(self.call("/api/export/preview", request)[0], 400)

    def test_reviewed_scope_cannot_be_bypassed_and_template_name_is_protected(self):
        _, _, _, preview = self.fixture()
        inspection = self.confirm(preview)
        changed = json.loads(json.dumps(inspection["config"]))
        changed["mappings"]["inventory"]["sheet"] = "原表1-1-南京库存"
        self.assertEqual(self.call("/api/analyze", {"workbookId": inspection["workbookId"], "config": changed})[0], 400)
        self.assertEqual(self.history_count(), 0)
        self.analyze(inspection)
        self.assertEqual(self.call("/api/import/confirm", {"token": preview["token"], "confirmed": True, "templateName": "虚构测试导入格式"})[0], 400)
        self.assertEqual(len(self.templates()), 1)

    def test_source_changed_between_review_and_confirmation_is_rejected(self):
        _, _, _, preview = self.fixture()
        workbook = openpyxl.load_workbook(self.paths[0])
        workbook["南京库存"]["D2"] = 99
        workbook.save(self.paths[0])
        workbook.close()
        status, body = self.call("/api/import/confirm", {"token": preview["token"], "confirmed": True, "templateName": "不应保存"})
        self.assertEqual(status, 400, body)
        self.assertEqual(self.history_count(), 0)
        self.assertEqual(self.templates(), [])

    def test_source_changed_after_confirmation_cannot_create_history_or_template(self):
        _, _, _, preview = self.fixture()
        inspection = self.confirm(preview)
        self.paths[1].write_bytes(b"changed by another application")
        status, body = self.call("/api/analyze", {"workbookId": inspection["workbookId"], "config": inspection["config"]})
        self.assertEqual(status, 400, body)
        self.assertEqual(self.history_count(), 0)
        self.assertEqual(self.templates(), [])

    def test_history_reopen_keeps_manifest_and_reanalysis_checks_originals(self):
        _, _, _, preview = self.fixture()
        result = self.analyze(self.confirm(preview))
        host.WORKBOOKS.clear()
        host.HISTORY_STORES.clear()
        detail = self.success("/api/history/" + result["id"])
        self.assertTrue(detail["sourceAvailable"], detail)
        self.assertEqual(detail["result"]["importManifest"], result["importManifest"])
        second = self.analyze(detail["inspection"])
        self.assertEqual(second["products"][0]["warehouse"], result["products"][0]["warehouse"])
        self.paths[0].unlink()
        status, body = self.call("/api/analyze", {"workbookId": detail["inspection"]["workbookId"], "config": detail["inspection"]["config"]})
        self.assertEqual(status, 400, body)
        self.assertEqual(self.history_count(), 2)

    def test_missing_original_makes_history_read_only_and_preserves_download(self):
        _, _, _, preview = self.fixture()
        result = self.analyze(self.confirm(preview))
        self.paths[1].unlink()
        host.WORKBOOKS.clear()
        host.HISTORY_STORES.clear()
        detail = self.success("/api/history/" + result["id"])
        self.assertFalse(detail["sourceAvailable"], detail)
        self.assertIsNone(detail["inspection"])
        self.assertEqual(detail["result"]["products"], result["products"])
        self.assertTrue(self.export_bytes(result).startswith(b"PK"))

    def test_excel_sources_explain_original_files_and_ignored_sheets(self):
        _, _, _, preview = self.fixture()
        before = {path: sha256(path) for path in self.paths}
        result = self.analyze(self.confirm(preview))
        workbook = openpyxl.load_workbook(io.BytesIO(self.export_bytes(result)), data_only=False)
        try:
            self.assertEqual(workbook.sheetnames, ["看盘总览", "商品明细", "库存与销量", "数据问题", "数据来源"])
            rows = list(workbook["数据来源"].values)
            original_rows = [row for row in rows if row[0] == "原始输入"]
            self.assertEqual(len(original_rows), 7)
            for path in self.paths:
                self.assertTrue(any(path.name in str(row[3]) for row in original_rows), path.name)
            ignored = [row for row in original_rows if row[2] == "ignore"]
            self.assertEqual(len(ignored), 1)
            self.assertIn("不纳入说明", ignored[0][3])
            self.assertIn("未计入分析", ignored[0][7])
            product_values = " ".join(str(value) for row in workbook["商品明细"].values for value in row if value is not None)
            self.assertNotIn("DO-NOT-COUNT", product_values)
        finally:
            workbook.close()
        self.assertEqual({path: sha256(path) for path in self.paths}, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
