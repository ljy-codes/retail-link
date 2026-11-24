"""每日售价／销量记录的独立 HTTP 分发行为测试；仅使用临时虚构数据。"""
from __future__ import annotations

from datetime import datetime
import hashlib
import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import openpyxl

PROJECT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(__file__).resolve().parent
DEV = PROJECT / "开发空间"
sys.path.insert(0, str(DEV))


class CaptureHandler:
    def __init__(self):
        self.status = None
        self.response = None

    def send_json(self, value, status=200):
        # Round-trip what would be sent over HTTP. Nonfinite numeric results
        # must not pass just because a Python object can hold them.
        self.response = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
        self.status = status


class DailyRecordsTests(unittest.TestCase):
    def test_separate_price_and_sales_can_merge_without_wiping_existing_values(self):
        original = self.create(salesQuantity=None)
        status, updated = self.call("POST", body={"record": {"date": original["date"],
            "product": original["product"], "shop": original["shop"], "price": None,
            "salesQuantity": 20}, "onConflict": "merge"})
        self.assertEqual(status, 200, updated)
        self.assertEqual(updated["record"]["price"], 12.5)
        self.assertEqual(updated["record"]["salesQuantity"], 20)
        status, error = self.call("POST", body={"record": {"date": original["date"],
            "product": original["product"], "shop": original["shop"], "price": 99}, "onConflict": "merge"})
        self.assertEqual(status, 400, error)
        _, saved = self.call("GET", path="/api/daily/"+original["id"])
        self.assertEqual(saved["record"]["price"], 12.5)

    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("backend.daily_records")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="每日记录自测-", dir=TEST_ROOT)
        self.path = Path(self.temporary.name)
        self.data = self.path / "persistent"
        self.workbooks = {}

    def tearDown(self):
        self.temporary.cleanup()

    def call(self, method="GET", path="/api/daily", body=None, query=None):
        handler = CaptureHandler()
        parsed_query = {key: value if isinstance(value, list) else [str(value)] for key, value in (query or {}).items()}
        try:
            handled = self.module.handle_daily_request(
                handler=handler, method=method, path=path, query=parsed_query,
                body=body, data_dir=self.data, lookup=lambda identity: self.workbooks[identity],
            )
        except ValueError as exc:
            # server.py deliberately maps validation errors into HTTP 400.
            handled = True
            handler.send_json({"error": str(exc)}, 400)
        self.assertTrue(handled, f"每日记录接口应处理 {method} {path}")
        self.assertIsNotNone(handler.status, "处理后必须返回明确 HTTP 状态")
        return handler.status, handler.response

    def create(self, **overrides):
        record = {"date": "2026-11-20", "product": "SKU-001", "name": "虚构商品甲",
                  "shop": "测试店", "price": 12.5, "salesQuantity": 8, "source": "人工自测"}
        record.update(overrides)
        status, result = self.call("POST", body={"record": record})
        self.assertEqual(status, 200, result)
        # The UI may wrap the persisted item as record/item while the contract
        # also permits returning the item directly.
        item = result.get("record", result.get("item", result))
        self.assertIn("id", item, result)
        return item

    def listing(self, **query):
        status, result = self.call(query=query)
        self.assertEqual(status, 200, result)
        self.assertIn("items", result)
        return result

    def long_fixture(self, rows=None):
        path = self.path / "每天售价-改版表.xlsx"
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "改版价格"
        sheet["A1"] = "虚构测试资料，不包含真实业务数据"
        headers = {"B3": "品号", "D3": "品名", "F3": "业务日期", "H3": "当日售价", "J3": "当日销量", "L3": "适用门店"}
        for cell, value in headers.items():
            sheet[cell] = value
        sample_rows = rows or [
            ("0001", "商品甲", datetime(2026, 11, 19), 10, 3, "甲店"),
            ("0001", "商品甲", datetime(2026, 11, 20), 12, 7, "甲店"),
            ("0002", "商品乙", datetime(2026, 11, 20), 0, 0, "甲店"),
        ]
        for row_number, values in enumerate(sample_rows, 4):
            for column, value in zip((2, 4, 6, 8, 10, 12), values):
                sheet.cell(row_number, column, value)
        workbook.save(path)
        self.workbooks["long"] = {"path": path, "filename": path.name, "sha256": "test-long"}
        return {"sheet": "改版价格", "headerRow": 3,
                "columns": {"product": "B", "name": "D", "date": "F", "price": "H", "salesQuantity": "J", "shop": "L"}}

    def snapshot_fixture(self):
        path = self.path / "每日另存价格.xlsx"
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "当日价格"
        sheet.append(["商品编码", "商品名", "最新售价", "本日销量"])
        sheet.append(["0001", "商品甲", 15.5, 9])
        workbook.save(path)
        self.workbooks["snapshot"] = {"path": path, "filename": path.name, "sha256": "test-snapshot"}
        return {"sheet": "当日价格", "headerRow": 1,
                "columns": {"product": "A", "name": "B", "price": "C", "salesQuantity": "D"}}

    def import_file(self, identity, mapping, preview=True, **extra):
        body = {"workbookId": identity, "mapping": mapping, "previewOnly": preview, "onConflict": "skip"}
        body.update(extra)
        return self.call("POST", "/api/daily/import", body=body)

    def test_prices_and_sales_remain_separate_for_each_day(self):
        self.create(date="2026-11-19", price=10, salesQuantity=3)
        self.create(date="2026-11-20", price=14, salesQuantity=9)
        items = self.listing()["items"]
        self.assertEqual(len(items), 2)
        by_day = {item["date"]: item for item in items}
        self.assertEqual((by_day["2026-11-19"]["price"], by_day["2026-11-19"]["salesQuantity"]), (10, 3))
        self.assertEqual((by_day["2026-11-20"]["price"], by_day["2026-11-20"]["salesQuantity"]), (14, 9))

    def test_same_product_in_two_shops_is_not_merged(self):
        self.create(shop="甲店", price=10)
        self.create(shop="乙店", price=14)
        self.assertEqual({(x["shop"], x["price"]) for x in self.listing()["items"]}, {("甲店", 10), ("乙店", 14)})

    def test_missing_numbers_remain_unknown_while_zero_is_kept(self):
        self.create(product="价格缺失", price=None, salesQuantity=0)
        self.create(product="销量缺失", price=0, salesQuantity=None)
        by_product = {item["product"]: item for item in self.listing()["items"]}
        self.assertIsNone(by_product["价格缺失"]["price"])
        self.assertEqual(by_product["价格缺失"]["salesQuantity"], 0)
        self.assertEqual(by_product["销量缺失"]["price"], 0)
        self.assertIsNone(by_product["销量缺失"]["salesQuantity"])

    def test_invalid_date_or_negative_or_nonfinite_numbers_are_rejected(self):
        for changes in ({"date": "2026-02-30"}, {"price": -1}, {"salesQuantity": -1}, {"price": "NaN"}, {"salesQuantity": "Infinity"}):
            with self.subTest(changes=changes):
                record = {"date": "2026-11-20", "product": "甲", "price": 5, "salesQuantity": 2, **changes}
                status, response = self.call("POST", body={"record": record})
                self.assertGreaterEqual(status, 400, response)
        self.assertEqual(self.listing()["items"], [])

    def test_filtering_matches_date_range_and_product(self):
        self.create(date="2026-11-18", product="A")
        self.create(date="2026-11-19", product="A")
        self.create(date="2026-11-20", product="A")
        self.create(date="2026-11-19", product="B")
        items = self.listing(start="2026-11-19", end="2026-11-20", product="A")["items"]
        self.assertEqual({(x["date"], x["product"]) for x in items}, {("2026-11-19", "A"), ("2026-11-20", "A")})

    def test_process_restart_retains_records(self):
        item = self.create(price=16.5, salesQuantity=4)
        program = """import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from backend.daily_records import handle_daily_request
class H:
 def send_json(self,value,status=200): print(json.dumps({'status':status,'value':value},ensure_ascii=False,allow_nan=False))
handle_daily_request(handler=H(),method='GET',path='/api/daily',query={},body=None,data_dir=Path(sys.argv[2]),lookup=lambda key:None)
"""
        completed = subprocess.run([sys.executable, "-c", program, str(DEV), str(self.data)], capture_output=True, text=True, check=True)
        response = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(response["status"], 200)
        self.assertEqual(response["value"]["items"][0]["id"], item["id"])
        self.assertEqual(response["value"]["items"][0]["price"], 16.5)

    def test_preview_does_not_save_and_custom_mapping_is_used(self):
        mapping = self.long_fixture()
        source_path = self.workbooks["long"]["path"]
        original_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        status, preview = self.import_file("long", mapping)
        self.assertEqual(status, 200, preview)
        self.assertEqual(self.listing()["items"], [], "预览不能提前写入正式日记录")
        status, saved = self.import_file("long", mapping, preview=False)
        self.assertEqual(status, 200, saved)
        items = self.listing()["items"]
        self.assertEqual(len(items), 3)
        self.assertIn(("0001", "2026-11-19", 10, 3), {(x["product"], x["date"], x["price"], x["salesQuantity"]) for x in items})
        self.assertIn(("0002", 0, 0), {(x["product"], x["price"], x["salesQuantity"]) for x in items})
        self.assertEqual(hashlib.sha256(source_path.read_bytes()).hexdigest(), original_hash, "导入不能改写原始工作簿")

    def test_duplicate_import_skips_instead_of_doubling_sales(self):
        mapping = self.long_fixture()
        self.assertEqual(self.import_file("long", mapping, preview=False)[0], 200)
        self.assertEqual(self.import_file("long", mapping, preview=False)[0], 200)
        items = self.listing()["items"]
        self.assertEqual(len(items), 3)
        self.assertEqual(sum(x["salesQuantity"] for x in items), 10)

    def test_explicit_replace_updates_same_day_but_keeps_other_dates(self):
        mapping = self.long_fixture()
        self.import_file("long", mapping, preview=False)
        path = self.workbooks["long"]["path"]
        workbook = openpyxl.load_workbook(path)
        workbook["改版价格"]["H5"] = 18
        workbook["改版价格"]["J5"] = 11
        workbook.save(path)
        status, response = self.import_file("long", mapping, preview=False, onConflict="replace")
        self.assertEqual(status, 200, response)
        by_key = {(x["date"], x["product"]): x for x in self.listing()["items"]}
        self.assertEqual((by_key[("2026-11-20", "0001")]["price"], by_key[("2026-11-20", "0001")]["salesQuantity"]), (18, 11))
        self.assertEqual((by_key[("2026-11-19", "0001")]["price"], by_key[("2026-11-19", "0001")]["salesQuantity"]), (10, 3))

    def test_same_day_replacement_retains_previous_values_in_revision_history(self):
        first = self.create(price=10, salesQuantity=3)
        replacement = {"date": "2026-11-20", "product": "SKU-001", "name": "虚构商品甲", "shop": "测试店", "price": 15, "salesQuantity": 7}
        status, response = self.call("POST", body={"record": replacement, "onConflict": "replace"})
        self.assertEqual(status, 200, response)
        self.assertEqual(response["record"]["id"], first["id"])
        status, revisions = self.call("GET", f"/api/daily/{first['id']}/revisions")
        self.assertEqual(status, 200, revisions)
        self.assertTrue(any(item["action"] == "replace" and item["record"]["price"] == 10 and item["record"]["salesQuantity"] == 3 for item in revisions["items"]))
        current = self.listing()["items"][0]
        self.assertEqual((current["price"], current["salesQuantity"]), (15, 7))

    def test_cumulative_sales_are_not_silently_imported_as_daily_quantity(self):
        mapping = self.snapshot_fixture()
        path = self.workbooks["snapshot"]["path"]
        workbook = openpyxl.load_workbook(path)
        workbook["当日价格"]["D1"] = "累计销量"
        workbook.save(path)
        status, preview = self.import_file("snapshot", mapping, snapshotDate="2026-11-20")
        self.assertTrue(status >= 400 or preview.get("issues"), preview)
        self.import_file("snapshot", mapping, preview=False, snapshotDate="2026-11-20")
        self.assertEqual(self.listing()["items"], [], "未确认的累计销量不能冒充当日销量入库")

    def test_snapshot_business_date_must_match_selected_sales_date_column(self):
        mapping = self.snapshot_fixture()
        path = self.workbooks["snapshot"]["path"]
        workbook = openpyxl.load_workbook(path)
        workbook["当日价格"]["D1"] = datetime(2026, 11, 19)
        workbook.save(path)
        status, preview = self.import_file("snapshot", mapping, snapshotDate="2026-11-20")
        self.assertTrue(status >= 400 or preview.get("issues"), preview)
        self.import_file("snapshot", mapping, preview=False, snapshotDate="2026-11-20")
        self.assertEqual(self.listing()["items"], [], "不能把昨天销量配上今天价格写成当天记录")

    def test_snapshot_requires_explicit_business_date(self):
        mapping = self.snapshot_fixture()
        status, response = self.import_file("snapshot", mapping, preview=False)
        self.assertTrue(status >= 400 or response.get("issues"), response)
        self.assertEqual(self.listing()["items"], [], "无日期的快照不能默认写成今天或文件修改日")
        status, response = self.import_file("snapshot", mapping, preview=False, snapshotDate="2026-11-20")
        self.assertEqual(status, 200, response)
        item = self.listing()["items"][0]
        self.assertEqual((item["date"], item["product"], item["price"], item["salesQuantity"]), ("2026-11-20", "0001", 15.5, 9))

    def test_invalid_import_rows_have_explicit_issues_and_do_not_become_zero(self):
        mapping = self.long_fixture(rows=[("坏数值", "甲", datetime(2026, 11, 20), "不是价格", 2, "甲店")])
        status, response = self.import_file("long", mapping)
        self.assertTrue(status >= 400 or response.get("issues"), response)
        self.import_file("long", mapping, preview=False)
        self.assertFalse(any(x.get("price") == 0 for x in self.listing()["items"]), "无法解析的价格不能伪装成0")

    def test_future_observations_are_not_silently_treated_as_current_data(self):
        mapping = self.long_fixture(rows=[("计划价", "甲", datetime(2099, 12, 31), 12, 2, "甲店")])
        status, response = self.import_file("long", mapping)
        self.assertTrue(status >= 400 or response.get("issues"), "未来日期需明确提醒，不应静默成为当天已发生记录")

    def test_delete_is_recoverable_and_does_not_touch_other_days(self):
        first = self.create(date="2026-11-19", price=10)
        second = self.create(date="2026-11-20", price=15)
        status, response = self.call("DELETE", f"/api/daily/{first['id']}")
        self.assertEqual(status, 200, response)
        self.assertEqual([x["id"] for x in self.listing()["items"]], [second["id"]])
        self.assertEqual([x["id"] for x in self.listing(deleted=1)["items"]], [first["id"]])
        status, response = self.call("POST", f"/api/daily/{first['id']}/restore", body={})
        self.assertEqual(status, 200, response)
        self.assertEqual({x["id"] for x in self.listing()["items"]}, {first["id"], second["id"]})
        self.assertEqual(self.listing(deleted=1)["items"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
