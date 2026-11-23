"""Public HTTP, immutable history and XLSX checks for product target assessment."""
from pathlib import Path
import copy
import hashlib
import importlib.util
import io
import json
import threading
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import unittest
import openpyxl
from test_per_product_assessment import FictionalAssessmentFixture, DEV

spec = importlib.util.spec_from_file_location("target_http_host", DEV / "server.py")
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


class ProductTargetHttpTests(FictionalAssessmentFixture):
    def setUp(self):
        super().setUp()
        host.APP_DIR = self.folder / "app"
        host.DATA_DIR = self.folder / "data"
        host.ORIGINAL_DIR = self.folder / "originals"
        host.ORIGINAL_DIR.mkdir()
        host.EXPORT_DIR = self.folder / "outputs"
        host.EXPORT_DIR.mkdir()
        for name in ("WORKBOOKS", "HISTORY_STORES", "EXPORT_SERVICES", "MIGRATION_SERVICES", "EXPORT_PREVIEWS", "DATASET_SERVICES"):
            getattr(host, name).clear()
        self.server = host.ThreadingHTTPServer(("127.0.0.1", 0), host.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        super().tearDown()

    def call(self, path, body=None):
        request = Request(self.url + path, data=json.dumps(body).encode() if body is not None else None,
                          headers={"Content-Type": "application/json"} if body is not None else {})
        try:
            with urlopen(request, timeout=30) as response:
                data = response.read()
                return response.status, json.loads(data) if "json" in response.headers.get("Content-Type", "") else data
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def ok(self, path, body=None):
        status, data = self.call(path, body)
        self.assertEqual(status, 200, data)
        return data

    def analyze_http(self, products=None, **kwargs):
        source, config = self.fixture(products or [("A", 100, 200), ("B", 100, 0)], **kwargs)
        before = source.read_bytes()
        info = self.ok("/api/open", {"path": str(source)})
        result = self.ok("/api/analyze", {"workbookId": info["workbookId"], "config": config})
        self.assertEqual(source.read_bytes(), before)
        return info, result

    def export(self, result, **extra):
        preview = self.ok("/api/export/preview", {"snapshotId": result["id"], "targetDir": str(host.EXPORT_DIR), "filename": result["id"] + ".xlsx", **extra})
        saved = self.ok("/api/export/xlsx", {"token": preview["token"]})
        payload = self.ok("/api/export/download/" + saved["operationId"])
        self.assertEqual(hashlib.sha256(payload).hexdigest(), saved["sha256"])
        return openpyxl.load_workbook(io.BytesIO(payload), data_only=True)

    def test_history_and_filtered_export_keep_product_scope_and_complete_overview(self):
        _, result = self.analyze_http()
        self.assertEqual(result["summary"]["achievement"], 1)
        self.assertEqual(result["summary"]["targetGap"], 85)
        stored = self.ok("/api/history/" + result["id"])["result"]
        self.assertEqual(stored["summary"]["belowTargetCount"], 1)
        self.assertEqual(stored["summary"]["assessmentScope"], "product")
        workbook = self.export(result, productIds=["B"])
        overview = {row[0]: row[1] for row in workbook["看盘总览"].iter_rows(min_row=2, values_only=True)}
        self.assertEqual(overview["逐款缺口合计"], 85)
        self.assertEqual(overview["已达标商品"], 1)
        self.assertEqual(overview["未达标商品"], 1)
        self.assertIn("完整分析快照", overview["导出范围"])
        rows = list(workbook["商品明细"].values)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][rows[0].index("商品标识")], "B")
        self.assertEqual(rows[1][rows[0].index("单品考核")], "未达标")
        workbook.close()

    def test_unknown_total_keeps_known_daily_portion_in_excel(self):
        _, result = self.analyze_http([("A", 100, 200), ("B", 100, 0), ("C", 100, None)])
        self.assertEqual(result["summary"]["unknownTargetCount"], 1)
        self.assertIsNone(result["summary"]["targetGap"])
        workbook = self.export(result)
        overview = {row[0]: row[1] for row in workbook["看盘总览"].iter_rows(min_row=2, values_only=True)}
        self.assertIsNone(overview["逐款缺口合计"])
        self.assertIsNone(overview["剩余每日需完成"])
        self.assertEqual(overview["已知商品缺口"], 85)
        self.assertEqual(overview["已知商品每日需完成"], 8.5)
        workbook.close()

    def test_old_snapshot_is_unchanged_and_reanalysis_creates_new_product_scope(self):
        info, result = self.analyze_http()
        old = copy.deepcopy(result)
        old["id"] = str(time.time_ns())
        old["appVersion"] = "0.1.5"
        for key in ("assessmentScope", "assessmentVersion", "metCount", "belowTargetCount", "unknownTargetCount", "knownTargetGap", "knownDailyRequired", "remainingDays", "dailyRequired"):
            old["summary"].pop(key, None)
        old["summary"]["targetGap"] = 0
        host.history_store().save(old)
        path = host.DATA_DIR / "history" / (old["id"] + ".json")
        before = path.read_bytes()
        host.HISTORY_STORES.clear()
        loaded = self.ok("/api/history/" + old["id"])["result"]
        self.assertNotIn("assessmentScope", loaded["summary"])
        self.assertEqual(loaded["summary"]["targetGap"], 0)
        workbook = self.export(loaded)
        overview = {row[0]: row[1] for row in workbook["看盘总览"].iter_rows(min_row=2, values_only=True)}
        self.assertEqual(overview["考核范围"], "历史记录口径")
        self.assertEqual(overview["距目标差额"], 0)
        workbook.close()
        newer = self.ok("/api/analyze", {"workbookId": info["workbookId"], "config": result["config"]})
        self.assertNotEqual(newer["id"], old["id"])
        self.assertEqual(newer["summary"]["targetGap"], 85)
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
