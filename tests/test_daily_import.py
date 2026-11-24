"""Daily paired-header and catalog workflows use only synthetic workbooks."""
from datetime import datetime
import hashlib
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.daily_records import DailyStore, handle_daily_request, inspect_daily, parse_import
from backend.daily_import import list_catalog, save_catalog, import_catalog, apply_catalog


class Handler:
    def send_json(self, value, status=200):
        self.value = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
        self.status = status


class DailyImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="价量适配自测-", dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = DailyStore(self.root / "data")
        self.entries = {}

    def paired(self, missing_year=False, duplicate=False):
        path = self.root / "价格-20261123.xlsx"
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "价量"
        for column, title in zip("ABCD", ("仓位", "商品id", "产品", "到岸价")):
            sheet[f"{column}1"] = title
            sheet.merge_cells(f"{column}1:{column}2")
        for columns, day in (("EF", datetime(2025, 11, 1)), ("GH", datetime(2025, 11, 2)), ("IJ", datetime(2099, 11, 3))):
            sheet[f"{columns[0]}1"] = "11.01" if missing_year else day
            sheet.merge_cells(f"{columns[0]}1:{columns[1]}1")
            sheet[f"{columns[0]}2"], sheet[f"{columns[1]}2"] = "销量", "售价"
        sheet["A3"], sheet["A5"] = "南京", "芜湖"
        sheet.merge_cells("A3:A4")
        for row, product in ((3, "0001"), (4, "0001" if duplicate else "0002"), (5, "0001")):
            sheet.cell(row, 2, product)
            sheet.cell(row, 3, "同名商品")
        for address, value in {"E3": 0, "F3": 0, "G3": 2, "H3": 12,
                               "F4": 9, "I4": 1, "J4": 10, "E5": 4}.items():
            sheet[address] = value
        extra = workbook.create_sheet("长表")
        extra.append(["商品编码", "日期", "售价"])
        extra.append(["9", "2025-11-01", 5])
        workbook.save(path)
        workbook.close()
        entry = {"path": path, "filename": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        self.entries["paired"] = entry
        return entry

    def catalog_fixture(self, conflict=False):
        path = self.root / "订货.xlsx"
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "订货"
        sheet.append(["商品ID", "商品名称", "销售规格", "仓库ID", "仓库名称", "商品总数", "商家报价（元）"])
        sheet.append(["0001", "同名商品", "1件", "1", "南京", 3, 10])
        sheet.append(["0002", "同名商品", "2件", "1", "南京", 4, 11])
        if conflict:
            sheet.append(["0001", "同名商品", "3件", "1", "南京", 2, 12])
        workbook.save(path)
        workbook.close()
        return {"path": path, "filename": path.name, "sha256": "synthetic-catalog"}

    def call(self, path, body=None, method="POST", query=None):
        handler = Handler()
        handle_daily_request(handler, method, path, query or {}, body, self.root / "data", lambda key: self.entries[key])
        return handler.value

    def settings(self):
        entry = self.paired()
        return {"workbookId": "paired", "mapping": inspect_daily(entry["path"], entry["filename"])["mapping"], "previewOnly": True, "onConflict": "skip"}

    def test_detects_paired_dates_and_does_not_guess_filename_year(self):
        entry = self.paired()
        inspected = inspect_daily(entry["path"], entry["filename"])
        mapping = inspected["mapping"]
        self.assertEqual(mapping["layout"], "paired_dates")
        self.assertEqual(mapping["datePairs"][0]["date"], "2025-11-01")
        by_sheet = {sheet["name"]: sheet["mapping"] for sheet in inspected["sheets"]}
        self.assertEqual(by_sheet["长表"]["layout"], "long")
        self.assertEqual(by_sheet["长表"]["sheet"], "长表")

    def test_merged_locations_zero_empty_and_future_are_distinct(self):
        body = self.settings()
        before = hashlib.sha256(self.entries["paired"]["path"].read_bytes()).hexdigest()
        parsed = self.call("/api/daily/import", body)
        self.assertEqual(parsed["validCount"], 4)
        self.assertEqual(parsed["invalidCount"], 1)
        self.assertTrue(any("未来" in item["message"] for item in parsed["issues"]))
        record = next(item for item in parsed["records"] if item["product"] == "0001" and item["shop"] == "南京" and item["date"] == "2025-11-01")
        self.assertEqual((record["price"], record["salesQuantity"]), (0, 0))
        second = next(item for item in parsed["records"] if item["product"] == "0002")
        self.assertEqual(second["shop"], "南京")
        self.assertIsNone(second["salesQuantity"])
        self.assertIn("priceCell", second["source"])
        self.assertEqual(len(self.store.list()["items"]), 0)
        self.assertEqual(hashlib.sha256(self.entries["paired"]["path"].read_bytes()).hexdigest(), before)

    def test_preview_commit_repeat_and_explicit_change_revision(self):
        body = self.settings()
        preview = self.call("/api/daily/import", body)
        saved = self.call("/api/daily/import", body | {"previewOnly": False, "previewFingerprint": preview["previewFingerprint"]})
        self.assertEqual(saved["saved"], 4)
        body["onConflict"] = "replace"
        repeated = self.call("/api/daily/import", body)
        self.assertEqual(repeated["reviewCounts"]["unchanged"], 4)
        self.call("/api/daily/import", body | {"previewOnly": False, "previewFingerprint": repeated["previewFingerprint"]})
        self.assertTrue(all(item["revision"] == 1 for item in self.store.list()["items"]))
        workbook = openpyxl.load_workbook(self.entries["paired"]["path"])
        workbook["价量"]["F3"] = 8
        workbook.save(self.entries["paired"]["path"])
        workbook.close()
        changed = self.call("/api/daily/import", body)
        conflict = next(item for item in changed["review"] if item["status"] == "conflict")
        self.assertEqual(conflict["differences"][0], {"field": "price", "label": "当日售价", "old": 0.0, "new": 8.0})
        self.call("/api/daily/import", body | {"previewOnly": False, "onConflict": "replace", "previewFingerprint": changed["previewFingerprint"]})
        updated = self.store.get(conflict["existing"]["id"])
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(self.store.revisions(updated["id"])["items"][0]["record"]["price"], 0)

    def test_stale_preview_rejected_atomically(self):
        body = self.settings()
        body["onConflict"] = "replace"
        preview = self.call("/api/daily/import", body)
        self.store.save({"date": "2025-11-01", "product": "0001", "shop": "南京", "price": 99})
        with self.assertRaisesRegex(ValueError, "重新预览"):
            self.call("/api/daily/import", body | {"previewOnly": False, "onConflict": "replace", "previewFingerprint": preview["previewFingerprint"]})
        self.assertEqual(self.store.list()["total"], 1)

    def test_missing_year_never_inferred_and_changed_year_rejected(self):
        entry = self.paired(missing_year=True)
        mapping = inspect_daily(entry["path"], entry["filename"])["mapping"]
        parsed = parse_import(entry, {"mapping": mapping})
        self.assertEqual(parsed["validCount"], 0)
        self.assertTrue(parsed["issues"])
        entry = self.paired()
        mapping = inspect_daily(entry["path"], entry["filename"])["mapping"]
        mapping["datePairs"][0]["date"] = "2026-11-01"
        parsed = parse_import(entry, {"mapping": mapping})
        self.assertFalse(any(item["date"] == "2026-11-01" for item in parsed["records"]))
        self.assertTrue(any("不一致" in item["message"] for item in parsed["issues"]))

    def test_in_file_duplicate_key_is_never_added_twice(self):
        entry = self.paired(duplicate=True)
        mapping = inspect_daily(entry["path"], entry["filename"])["mapping"]
        result = parse_import(entry, {"mapping": mapping})
        self.assertEqual(result["duplicateCount"], 1)
        keys = [(item["date"], item["product"], item["shop"]) for item in result["records"]]
        self.assertEqual(len(keys), len(set(keys)))

    def test_catalog_preview_persistence_explicit_alias_without_name_merge(self):
        entry = self.catalog_fixture()
        preview = import_catalog(self.store, entry, {"previewOnly": True, "sheet": "订货"})
        self.assertEqual(list_catalog(self.store)["total"], 0)
        self.assertEqual(preview["reviewCounts"]["new"], 2)
        import_catalog(self.store, entry, {"previewOnly": False, "previewFingerprint": preview["previewFingerprint"]})
        self.assertEqual({row["product"] for row in list_catalog(DailyStore(self.root / "data"))["items"]}, {"0001", "0002"})
        row = list_catalog(self.store)["items"][0]
        save_catalog(self.store, row | {"product": "STANDARD-1", "shop": "南京自有仓"}, expected_revision=1)
        body = self.settings()
        parsed = self.call("/api/daily/import", body)
        self.assertTrue(any(item["product"] == "STANDARD-1" and item["shop"] == "南京自有仓" for item in parsed["records"]))
        self.assertTrue(any(item["product"] == "0001" and item["shop"] == "芜湖" for item in parsed["records"]))
        self.assertTrue(any(item["product"] == "0002" for item in parsed["records"]))
        with self.assertRaisesRegex(ValueError, "已变化"):
            save_catalog(self.store, row, expected_revision=1)

    def test_catalog_different_specs_become_distinct_products(self):
        preview = import_catalog(self.store, self.catalog_fixture(conflict=True), {"previewOnly": True})
        self.assertEqual(preview["validCount"], 3)
        variants = [row for row in preview["records"] if row["sourceProduct"] == "0001"]
        self.assertEqual(len({row["product"] for row in variants}), 2)
        self.assertEqual(preview["issues"], [])

    def test_catalog_commit_requires_current_preview(self):
        entry = self.catalog_fixture()
        with self.assertRaisesRegex(ValueError, "先预览"):
            import_catalog(self.store, entry, {"previewOnly": False})
        preview = import_catalog(self.store, entry, {"previewOnly": True})
        save_catalog(self.store, {"sourceProduct": "0001", "sourceShop": "南京", "product": "EDITED"})
        with self.assertRaisesRegex(ValueError, "重新预览"):
            import_catalog(self.store, entry, {"previewOnly": False, "previewFingerprint": preview["previewFingerprint"]})
        self.assertEqual(list_catalog(self.store)["total"], 1)

    def test_name_only_change_is_previewed_and_archived_when_replaced(self):
        body = self.settings()
        body["onConflict"] = "replace"
        first = self.call("/api/daily/import", body)
        self.call("/api/daily/import", body | {"previewOnly": False, "previewFingerprint": first["previewFingerprint"]})
        workbook = openpyxl.load_workbook(self.entries["paired"]["path"])
        workbook["价量"]["C3"] = "新商品名称"
        workbook.save(self.entries["paired"]["path"])
        workbook.close()
        changed = self.call("/api/daily/import", body)
        conflicts = [row for row in changed["review"] if row["status"] == "conflict"]
        self.assertEqual(len(conflicts), 2)
        for row in conflicts:
            self.assertEqual(row["differences"], [{"field": "name", "label": "商品名称", "old": "同名商品", "new": "新商品名称"}])
        self.call("/api/daily/import", body | {"previewOnly": False, "previewFingerprint": changed["previewFingerprint"]})
        saved = self.store.get(conflicts[0]["existing"]["id"])
        self.assertEqual(saved["name"], "新商品名称")
        self.assertEqual(saved["revision"], 2)
        self.assertEqual(self.store.revisions(saved["id"])["items"][0]["record"]["name"], "同名商品")

    def test_region_header_keeps_same_product_aliases_separate(self):
        entry = self.catalog_fixture()
        workbook = openpyxl.load_workbook(entry["path"])
        sheet = workbook["订货"]
        sheet["E1"] = "地区"
        sheet.append(["0001", "同名商品", "1件", "2", "芜湖", 2, 10])
        workbook.save(entry["path"])
        workbook.close()
        preview = import_catalog(self.store, entry, {"previewOnly": True})
        import_catalog(self.store, entry, {"previewOnly": False, "previewFingerprint": preview["previewFingerprint"]})
        rows = list_catalog(self.store)["items"]
        self.assertEqual({row["sourceShop"] for row in rows if row["sourceProduct"] == "0001"}, {"南京", "芜湖"})
        row = next(row for row in rows if row["sourceProduct"] == "0001" and row["sourceShop"] == "南京")
        save_catalog(self.store, row | {"product": "NANJING-ONLY"}, expected_revision=1)
        parsed = self.call("/api/daily/import", self.settings())
        self.assertTrue(any(row["product"] == "NANJING-ONLY" and row["shop"] == "南京" for row in parsed["records"]))
        self.assertTrue(any(row["product"] == "0001" and row["shop"] == "芜湖" for row in parsed["records"]))

    def test_value_header_and_date_group_are_validated(self):
        body = self.settings()
        mapping = body["mapping"]
        wrong_header = json.loads(json.dumps(mapping))
        wrong_header["valueHeaderRow"] = 3
        wrong_header["dataStartRow"] = 4
        with self.assertRaisesRegex(ValueError, "不是销量表头"):
            parse_import(self.entries["paired"], {"mapping": wrong_header})
        wrong_columns = json.loads(json.dumps(mapping))
        wrong_columns["datePairs"][0]["salesQuantity"] = "F"
        wrong_columns["datePairs"][0]["price"] = "E"
        with self.assertRaisesRegex(ValueError, "不是销量表头"):
            parse_import(self.entries["paired"], {"mapping": wrong_columns})
        wrong_group = json.loads(json.dumps(mapping))
        wrong_group["datePairs"][0]["price"] = "H"
        parsed = parse_import(self.entries["paired"], {"mapping": wrong_group})
        self.assertTrue(any("不属于所选日期组" in row["message"] for row in parsed["issues"]))
        self.assertFalse(any(row["date"] == "2025-11-01" for row in parsed["records"]))
        workbook = openpyxl.load_workbook(self.entries["paired"]["path"])
        workbook["价量"].insert_rows(2)
        workbook["价量"]["E3"], workbook["价量"]["F3"] = "销量", "售价"
        workbook.save(self.entries["paired"]["path"])
        workbook.close()
        # An explicitly moved value header is read from that row, not ignored.
        moved = json.loads(json.dumps(mapping))
        moved.update(valueHeaderRow=3, dataStartRow=4)
        moved["datePairs"] = moved["datePairs"][:1]
        moved_result = parse_import(self.entries["paired"], {"mapping": moved})
        self.assertTrue(moved_result["records"])

    def test_paired_commit_requires_preview_and_binds_conflict_policy(self):
        body = self.settings()
        with self.assertRaisesRegex(ValueError, "先预览"):
            self.call("/api/daily/import", body | {"previewOnly": False})
        preview = self.call("/api/daily/import", body)
        with self.assertRaisesRegex(ValueError, "重新预览"):
            self.call("/api/daily/import", body | {"previewOnly": False, "onConflict": "replace", "previewFingerprint": preview["previewFingerprint"]})
        changed_mapping = json.loads(json.dumps(body))
        changed_mapping["mapping"]["columns"]["name"] = ""
        with self.assertRaisesRegex(ValueError, "重新预览"):
            self.call("/api/daily/import", changed_mapping | {"previewOnly": False, "previewFingerprint": preview["previewFingerprint"]})
        self.assertEqual(self.store.list()["total"], 0)

    def test_catalog_policy_change_invalidates_preview(self):
        entry = self.catalog_fixture()
        preview = import_catalog(self.store, entry, {"previewOnly": True, "onConflict": "skip"})
        with self.assertRaisesRegex(ValueError, "重新预览"):
            import_catalog(self.store, entry, {"previewOnly": False, "onConflict": "replace", "previewFingerprint": preview["previewFingerprint"]})
        self.assertEqual(list_catalog(self.store)["total"], 0)

    def test_fill_only_means_missing_old_value_receives_known_new_value(self):
        original = self.store.save({"date": "2025-11-01", "product": "1", "name": "甲", "price": 10, "salesQuantity": 2})["record"]
        incomplete = {"date": "2025-11-01", "product": "1", "name": "甲", "price": None, "salesQuantity": 2}
        preview = self.store.reviewed_import([incomplete], "replace")
        self.assertEqual(preview["review"][0]["status"], "conflict")
        self.assertEqual(preview["review"][0]["differences"], [{"field": "price", "label": "当日售价", "old": 10.0, "new": None}])
        self.store.reviewed_import([incomplete], "merge", preview_only=False)
        self.assertEqual(self.store.get(original["id"])["price"], 10)
        self.store.reviewed_import([incomplete], "replace", preview_only=False, expected_fingerprint=preview["previewFingerprint"])
        self.assertIsNone(self.store.get(original["id"])["price"])
        fill = self.store.reviewed_import([incomplete | {"price": 12}], "merge")
        self.assertEqual(fill["review"][0]["status"], "fill")


if __name__ == "__main__":
    unittest.main()
