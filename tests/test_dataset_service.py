"""Behavior checks for multi-workbook import using disposable fictional data."""
from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class DatasetBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("backend.dataset_service")
        cls.analyzer = importlib.import_module("backend.analyzer")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dataset-", dir=TEST_DIR)
        self.root = Path(self.temp.name)
        self.data_dir = self.root / "data"
        self.entries = {}
        self.service = self.module.DataSetService(self.data_dir, lambda identity: self.entries[identity])

    def tearDown(self):
        self.temp.cleanup()

    def workbook(self, identity, sheets, filename=None):
        location = self.root / identity
        location.mkdir(exist_ok=True)
        path = location / (filename or identity + ".xlsx")
        workbook = openpyxl.Workbook()
        workbook.remove(workbook.active)
        for name, rows in sheets.items():
            sheet = workbook.create_sheet(name)
            for row in rows:
                sheet.append(row)
        workbook.save(path)
        workbook.close()
        self.register(identity, path)
        return path

    def register(self, identity, path):
        self.entries[identity] = {"path": Path(path), "filename": Path(path).name,
                                  "sha256": digest(path), "size": Path(path).stat().st_size}

    def stage(self, ids, mapping=None, baseline=None):
        staged = self.service.stage(ids, baselineId=baseline)
        selections = []
        for sheet in staged["sheets"]:
            choice = (mapping or {}).get(sheet["sheet"], {})
            selections.append({"sourceId": sheet["sourceId"], "role": choice.get("role", "ignore"),
                               "headerRow": choice.get("headerRow", 1), "columns": choice.get("columns", {}),
                               "unit": choice.get("unit", "small"),
                               "snapshotDate": choice.get("snapshotDate", "2026-11-20" if choice.get("role") == "inventory" else ""),
                               "warehouse": choice.get("warehouse", "")})
        config = dict(staged.get("config", {}), asOf="2026-11-20", period="2026-11",
                      amountMode="calculated", averageDays=1, inventoryComparable=True)
        return staged, selections, config

    def materialize(self, staged, selections, config):
        preview = self.service.preview(staged["sessionId"], selections, config)
        self.assertTrue(preview.get("canContinue", True), json.dumps(preview, ensure_ascii=False))
        generated = self.service.materialize(preview["token"])
        self.assertTrue(Path(generated["path"]).is_file())
        return preview, generated

    def fixture(self):
        self.workbook("inventory", {
            "南京库存": [["商品标识", "商品名称", "仓库", "箱规", "库存数量", "单价"],
                        ["SKU-001", "虚构商品甲", "南京", 6, 12, 10]],
            "芜湖库存": [["商品标识", "商品名称", "仓库", "箱规", "库存数量", "单价"],
                        ["SKU-001", "虚构商品甲", "芜湖", 6, 18, 10]],
            "说明": [["本表仅用于测试，不应纳入业务数据"]],
        })
        headers = ["商品标识", "仓库", "日期", "数量", "单价", "供价"]
        self.workbook("movements", {
            "销售明细": [headers, ["SKU-001", "南京", "2026-11-19", 4, 10, 8],
                        ["SKU-001", "南京", "2026-11-20", 0, 10, 8]],
            "南京出库": [headers, ["SKU-001", "南京", "2026-11-19", 2, 10, 8]],
            "芜湖出库": [headers, ["SKU-001", "芜湖", "2026-11-19", 3, 10, 8]],
            "订单明细": [headers, ["SKU-001", "南京", "2026-11-19", 5, 60, 48]],
            "在途明细": [headers, ["SKU-001", "南京", "2026-11-20", 2, 60, 48]],
            "货损": [headers, ["SKU-001", "南京", "2026-11-19", 1, 10, 8]],
        })
        self.workbook("plans", {"月计划": [["商品标识", "商品名称", "计划金额"],
                                            ["SKU-001", "虚构商品甲", 1000]]})
        inventory = {"role": "inventory", "columns": {"product": "A", "name": "B", "warehouse": "C",
                      "pack": "D", "stockSmall": "E", "basePrice": "F"}}
        mapping = {"南京库存": inventory, "芜湖库存": inventory,
                   "月计划": {"role": "summary", "columns": {"product": "A", "name": "B", "plan": "C"}}}
        for title, role, unit in [("销售明细", "sales", "small"), ("南京出库", "outbound", "small"),
                                  ("芜湖出库", "outbound", "small"), ("订单明细", "orders", "large"),
                                  ("在途明细", "transit", "large"), ("货损", "loss", "small")]:
            mapping[title] = {"role": role, "unit": unit, "columns": {"product": "A", "warehouse": "B",
                "date": "C", "quantity": "D", "basePrice": "E", "supplyPrice": "F"}}
        return mapping

    def assert_rejected(self, staged, selections, config):
        try:
            preview = self.service.preview(staged["sessionId"], selections, config)
        except ValueError:
            return
        self.assertFalse(preview.get("canContinue", True), json.dumps(preview, ensure_ascii=False))
        with self.assertRaises(ValueError):
            self.service.materialize(preview["token"])

    def test_multiple_workbooks_and_warehouses_produce_complete_weighted_result(self):
        mapping = self.fixture()
        staged, selections, config = self.stage(["inventory", "movements", "plans"], mapping)
        preview, generated = self.materialize(staged, selections, config)
        result = self.analyzer.analyze_workbook(generated["path"], generated["config"])
        self.assertEqual(len(result["products"]), 1)
        item = result["products"][0]
        self.assertEqual(item["stockSmall"], 30)
        self.assertEqual(item["stockAmount"], 300)
        self.assertEqual(item["outboundQuantity"], 5)
        self.assertEqual(item["outboundAmount"], 50)
        self.assertEqual(item["supplyAmount"], 40)
        self.assertAlmostEqual(item["discountRate"], .8)
        self.assertEqual(item["completed"], 300)
        self.assertAlmostEqual(item["achievement"], .3)
        self.assertEqual(item["targetGap"], 550)
        self.assertEqual(item["transitSmall"], 12)
        self.assertEqual(item["yesterdaySales"], 4)
        self.assertEqual(item["salesQuantity"], 4)
        self.assertEqual(preview["selectedCount"], 9)
        self.assertEqual(preview["excludedCount"], 1)
        self.assertTrue(generated["selectionManifest"])
        self.assertEqual(len(generated["inputFiles"]), 3)

    def test_identical_file_hash_is_not_double_counted(self):
        mapping = self.fixture()
        original = self.entries["inventory"]["path"]
        copy = self.root / "inventory-copy.xlsx"
        shutil.copy2(original, copy)
        self.register("copy", copy)
        staged, selections, config = self.stage(["inventory", "copy"], mapping)
        self.assertEqual(len(staged["sheets"]), 3)
        _, generated = self.materialize(staged, selections, config)
        result = self.analyzer.analyze_workbook(generated["path"], generated["config"])
        self.assertEqual(result["products"][0]["stockSmall"], 30)

    def test_same_filename_files_have_distinct_source_ids(self):
        self.workbook("one", {"表": [["商品", "库存"], ["甲", 3]]}, "库存.xlsx")
        self.workbook("two", {"表": [["商品", "库存"], ["乙", 5]]}, "库存.xlsx")
        staged = self.service.stage(["one", "two"])
        self.assertEqual(len(staged["sheets"]), 2)
        self.assertEqual(len({row["sourceId"] for row in staged["sheets"]}), 2)

    def test_more_than_eight_files_are_rejected(self):
        for index in range(9):
            self.workbook(str(index), {"表": [["商品", "库存"], [f"SKU-{index}", index]]})
        with self.assertRaises(ValueError):
            self.service.stage(list(self.entries))

    def test_all_ignored_or_omitted_source_does_not_produce_success(self):
        self.fixture()
        staged, selections, config = self.stage(["inventory"])
        self.assert_rejected(staged, selections, config)
        self.assert_rejected(staged, selections[:-1], config)

    def test_same_physical_sheet_cannot_be_selected_twice(self):
        mapping = self.fixture()
        staged, selections, config = self.stage(["inventory"], mapping)
        second_role = dict(selections[0], role="sales", columns={"product": "A", "quantity": "E"})
        self.assert_rejected(staged, selections + [second_role], config)

    def test_duplicate_plan_sources_do_not_double_the_target(self):
        self.workbook("one", {"计划一": [["商品", "计划"], ["SKU-001", 1000]],
                              "计划二": [["商品", "计划"], ["SKU-001", 1000]]})
        choice = {"role": "summary", "columns": {"product": "A", "plan": "B"}}
        staged, selections, config = self.stage(["one"], {"计划一": choice, "计划二": choice})
        self.assert_rejected(staged, selections, config)

    def test_two_inventory_snapshots_for_same_product_and_warehouse_are_not_added(self):
        self.workbook("one", {"库存一": [["商品", "仓库", "库存"], ["SKU-001", "南京", 10]],
                              "库存二": [["商品", "仓库", "库存"], ["SKU-001", "南京", 12]]})
        choice = {"role": "inventory", "columns": {"product": "A", "warehouse": "B", "stockSmall": "C"}}
        staged, selections, config = self.stage(["one"], {"库存一": choice, "库存二": choice})
        self.assert_rejected(staged, selections, config)

    def test_future_stock_is_blocked_before_analysis(self):
        mapping = self.fixture()
        staged, selections, config = self.stage(["inventory"], mapping)
        for selection in selections:
            if selection["role"] == "inventory": selection["snapshotDate"] = "2026-11-21"
        preview = self.service.preview(staged["sessionId"], selections, config)
        self.assertFalse(preview["canContinue"])
        self.assertTrue(any(issue["code"] == "FUTURE_STOCK_DATE" for issue in preview["issues"]))

    def test_older_stock_retains_explicit_date_and_warns(self):
        mapping = self.fixture()
        staged, selections, config = self.stage(["inventory"], mapping)
        for selection in selections:
            if selection["role"] == "inventory": selection["snapshotDate"] = "2026-11-19"
        preview = self.service.preview(staged["sessionId"], selections, config)
        self.assertTrue(preview["canContinue"])
        self.assertTrue(any(issue["code"] == "OLDER_STOCK_SNAPSHOT" for issue in preview["issues"]))
        stock = [item for item in preview["selectionManifest"] if item["role"] == "inventory"]
        self.assertTrue(all(item["resolvedSnapshotDate"] == "2026-11-19" for item in stock))

    def test_inventory_needs_a_known_business_date(self):
        mapping = self.fixture()
        staged, selections, config = self.stage(["inventory"], mapping)
        for selected in selections:
            selected["snapshotDate"] = ""
        self.assert_rejected(staged, selections, config)

    def test_different_warehouse_inventory_snapshots_cannot_be_combined_across_dates(self):
        mapping = self.fixture()
        mapping["芜湖库存"] = dict(mapping["芜湖库存"], snapshotDate="2026-11-19")
        staged, selections, config = self.stage(["inventory"], mapping)
        self.assert_rejected(staged, selections, config)

    def test_inventory_date_columns_are_checked_per_row(self):
        self.workbook("source", {"库存": [["商品", "仓库", "库存", "日期"],
            ["SKU-001", "南京", 10, "2026-11-19"], ["SKU-001", "芜湖", 12, "2026-11-20"]]})
        mapping = {"库存": {"role": "inventory", "snapshotDate": "", "columns": {
            "product": "A", "warehouse": "B", "stockSmall": "C", "date": "D"}}}
        staged, selections, config = self.stage(["source"], mapping)
        self.assert_rejected(staged, selections, config)

    def test_empty_sales_cells_are_missing_but_numeric_zero_is_retained(self):
        self.workbook("source", {"销售": [["商品", "日期", "数量", "单价"],
            ["零销量", "2026-11-19", 0, 10], ["未知量", "2026-11-19", None, 10]]})
        choice = {"role": "sales", "columns": {"product": "A", "date": "B", "quantity": "C", "basePrice": "D"}}
        staged, selections, config = self.stage(["source"], {"销售": choice})
        preview = self.service.preview(staged["sessionId"], selections, config)
        if not preview.get("canContinue", True):
            self.assertTrue(preview["issues"])
            return
        generated = self.service.materialize(preview["token"])
        result = self.analyzer.analyze_workbook(generated["path"], generated["config"])
        by_id = {item["id"]: item for item in result["products"]}
        self.assertEqual(by_id["零销量"]["salesQuantity"], 0)
        self.assertIsNone(by_id.get("未知量", {}).get("salesQuantity"))

    def test_missing_dates_require_explicit_snapshot_date(self):
        self.workbook("source", {"报单": [["商品", "数量", "单价"], ["SKU-001", 2, 10]]})
        choice = {"role": "orders", "unit": "large", "columns": {"product": "A", "quantity": "B", "basePrice": "C"}}
        staged, selections, config = self.stage(["source"], {"报单": choice})
        self.assert_rejected(staged, selections, config)
        selections[0]["snapshotDate"] = "2026-11-19"
        preview, generated = self.materialize(staged, selections, config)
        self.assertTrue(generated["selectionManifest"])

    def test_large_unit_movements_require_pack_and_convert_both_quantity_and_price(self):
        self.workbook("source", {"出库": [["商品", "日期", "数量", "单价"],
                                            ["SKU-001", "2026-11-19", 2, 60]],
                                 "规格": [["商品", "箱规", "大单位价格"], ["SKU-001", 6, 60]]})
        mapping = {"出库": {"role": "outbound", "unit": "large", "columns": {"product": "A", "date": "B", "quantity": "C", "basePrice": "D"}}}
        staged, selections, config = self.stage(["source"], mapping)
        self.assert_rejected(staged, selections, config)
        spec = next(row for row in staged["sheets"] if row["sheet"] == "规格")
        for selected in selections:
            if selected["sourceId"] == spec["sourceId"]:
                selected.update(role="products", columns={"product": "A", "pack": "B", "basePriceLarge": "C"})
        _, generated = self.materialize(staged, selections, config)
        item = self.analyzer.analyze_workbook(generated["path"], generated["config"])["products"][0]
        self.assertEqual(item["outboundQuantity"], 12)
        self.assertEqual(item["outboundAmount"], 120)

    def test_small_unit_orders_convert_case_quantity_and_case_price_consistently(self):
        self.workbook("source", {"报单": [["商品", "日期", "数量", "单价"],
            ["SKU-001", "2026-11-19", 6, 10]], "规格": [["商品", "箱规", "大单位价格"], ["SKU-001", 6, 60]]})
        mapping = {"报单": {"role": "orders", "unit": "small", "columns": {"product": "A", "date": "B", "quantity": "C", "basePrice": "D"}},
                   "规格": {"role": "products", "columns": {"product": "A", "pack": "B", "basePriceLarge": "C"}}}
        staged, selections, config = self.stage(["source"], mapping)
        _, generated = self.materialize(staged, selections, config)
        item = self.analyzer.analyze_workbook(generated["path"], generated["config"])["products"][0]
        self.assertEqual(item["completed"], 60)

    def test_source_changed_after_stage_or_preview_requires_reimport(self):
        mapping = self.fixture()
        staged, selections, config = self.stage(["inventory"], mapping)
        source = self.entries["inventory"]["path"]
        source.write_bytes(source.read_bytes() + b"external edit")
        with self.assertRaises(ValueError):
            self.service.preview(staged["sessionId"], selections, config)

    def test_source_changed_after_confirmation_preview_is_not_materialized(self):
        mapping = self.fixture()
        staged, selections, config = self.stage(["inventory"], mapping)
        preview = self.service.preview(staged["sessionId"], selections, config)
        self.assertTrue(preview.get("canContinue", True), preview)
        source = self.entries["inventory"]["path"]
        source.write_bytes(source.read_bytes() + b"changed after preview")
        with self.assertRaises(ValueError):
            self.service.materialize(preview["token"])

    def test_explicit_zero_sales_is_an_observed_record(self):
        self.workbook("zero", {"销售": [["商品", "日期", "数量", "单价"],
                                         ["SKU-001", "2026-11-19", 0, 10]]})
        mapping = {"销售": {"role": "sales", "columns": {"product": "A", "date": "B", "quantity": "C", "basePrice": "D"}}}
        staged, selections, config = self.stage(["zero"], mapping)
        _, generated = self.materialize(staged, selections, config)
        item = self.analyzer.analyze_workbook(generated["path"], generated["config"])["products"][0]
        self.assertEqual(item["salesQuantity"], 0)
        self.assertEqual(item["averageSales"], 0)

    def test_materialization_keeps_user_source_bytes_unchanged(self):
        mapping = self.fixture()
        before = {identity: digest(item["path"]) for identity, item in self.entries.items()}
        staged, selections, config = self.stage(list(self.entries), mapping)
        _, generated = self.materialize(staged, selections, config)
        self.assertEqual(before, {identity: digest(item["path"]) for identity, item in self.entries.items()})
        self.assertIn("provenance", generated)

    def test_provenance_connects_canonical_rows_to_original_rows(self):
        mapping = self.fixture()
        staged, selections, config = self.stage(["inventory", "movements", "plans"], mapping)
        _, generated = self.materialize(staged, selections, config)
        workbook = openpyxl.load_workbook(generated["path"], data_only=True)
        try:
            original_by_name = {item["filename"]: item["path"] for item in self.entries.values()}
            real_origins = [row for row in generated["provenance"] if row.get("sourceId")]
            self.assertTrue(real_origins)
            for origin in real_origins:
                self.assertIn(origin["canonicalSheet"], workbook.sheetnames)
                self.assertGreaterEqual(origin["canonicalRow"], 2)
                self.assertEqual(workbook[origin["canonicalSheet"]].cell(origin["canonicalRow"], 1).value, origin["product"])
                original = openpyxl.load_workbook(original_by_name[origin["filename"]], data_only=True)
                try:
                    self.assertEqual(original[origin["sheet"]].cell(origin["row"], 1).value, origin["product"])
                finally:
                    original.close()
            self.assertFalse(any(row["sheet"] == "说明" for row in real_origins))
        finally:
            workbook.close()

    def baseline_fixture(self):
        self.workbook("baseline", {"计划表": [["商品", "计划金额"], ["SKU-001", 1000]]}, "运营数据.xlsx")
        mapping = {"计划表": {"role": "summary", "columns": {"product": "A", "plan": "B"}}}
        staged, selections, config = self.stage(["baseline"], mapping)
        preview, generated = self.materialize(staged, selections, config)
        saved = self.service.accept(preview["token"], "测试输入模板")
        templates = self.service.templates()
        if isinstance(templates, dict):
            templates = templates.get("templates", [])
        selected = next(item for item in templates if item["name"] == "测试输入模板")
        return selected.get("id", selected.get("templateId")), mapping

    def format_changes(self, staged, selections, config):
        preview = self.service.preview(staged["sessionId"], selections, config)
        return staged.get("formatChanges", []) or preview.get("formatChanges", [])

    def test_preview_without_confirmation_does_not_store_a_template(self):
        self.workbook("source", {"计划": [["商品", "计划金额"], ["SKU-001", 1000]]})
        mapping = {"计划": {"role": "summary", "columns": {"product": "A", "plan": "B"}}}
        staged, selections, config = self.stage(["source"], mapping)
        self.service.preview(staged["sessionId"], selections, config)
        templates = self.service.templates()
        self.assertEqual(templates.get("templates", []) if isinstance(templates, dict) else templates, [])

    def test_changed_data_values_do_not_trigger_layout_change(self):
        baseline, mapping = self.baseline_fixture()
        self.workbook("updated", {"计划表": [["商品", "计划金额"], ["SKU-001", 1001]]}, "运营数据.xlsx")
        staged, selections, config = self.stage(["updated"], mapping, baseline)
        self.assertEqual(self.format_changes(staged, selections, config), [])

    def test_daily_filename_collisions_do_not_replace_another_source_mapping(self):
        self.workbook("day1", {"计划表": [["商品", "计划金额"], ["SKU-001", 1000]]}, "20261119计划.xlsx")
        self.workbook("day2", {"计划表": [["商品", "计划金额"], ["SKU-002", 2000]]}, "20261120计划.xlsx")
        mapping = {"计划表": {"role": "summary", "columns": {"product": "A", "plan": "B"}}}
        staged, selections, config = self.stage(["day1", "day2"], mapping)
        self.assertEqual(len({item["logicalSource"] for item in staged["sheets"]}), 2)
        self.assertTrue(any(item.get("code") == "LOGICAL_SOURCE_COLLISION" for item in staged["warnings"]))
        preview, generated = self.materialize(staged, selections, config)
        result = self.analyzer.analyze_workbook(generated["path"], generated["config"])
        self.assertEqual({row["id"] for row in result["products"]}, {"SKU-001", "SKU-002"})
        self.service.accept(preview["token"], "每日文件同名范围")
        persisted = json.loads((self.data_dir / "import_templates.json").read_text(encoding="utf-8"))
        saved = next(item for item in persisted["templates"] if item["name"] == "每日文件同名范围")
        self.assertEqual(len(saved["structures"]), 2)
        self.assertEqual(len(saved["selections"]), 2)

    def test_added_removed_and_reordered_headers_are_reported(self):
        baseline, mapping = self.baseline_fixture()
        variants = [([["商品", "计划金额", "备注"], ["SKU-001", 1000, "测试"]], mapping),
                    ([["商品"], ["SKU-001"]], mapping),
                    ([["计划金额", "商品"], [1000, "SKU-001"]],
                     {"计划表": {"role": "summary", "columns": {"product": "B", "plan": "A"}}})]
        for index, (rows, chosen_mapping) in enumerate(variants):
            with self.subTest(index=index):
                identity = "changed" + str(index)
                self.workbook(identity, {"计划表": rows}, "运营数据.xlsx")
                staged, selections, config = self.stage([identity], chosen_mapping, baseline)
                self.assertTrue(self.format_changes(staged, selections, config))

    def test_merged_header_change_is_reported(self):
        baseline, mapping = self.baseline_fixture()
        path = self.workbook("merged", {"计划表": [["商品", "计划金额"], ["SKU-001", 1000]]}, "运营数据.xlsx")
        wb = openpyxl.load_workbook(path)
        wb.active.merge_cells("A1:B1")
        wb.save(path)
        wb.close()
        self.register("merged", path)
        staged, selections, config = self.stage(["merged"], mapping, baseline)
        self.assertTrue(self.format_changes(staged, selections, config))

    def test_ignoring_changed_preview_keeps_original_template(self):
        baseline, mapping = self.baseline_fixture()
        before = json.dumps(self.service.templates(), ensure_ascii=False, sort_keys=True)
        self.workbook("changed", {"计划表": [["商品", "计划金额", "备注"], ["SKU-001", 1000, "测试"]]}, "运营数据.xlsx")
        staged, selections, config = self.stage(["changed"], mapping, baseline)
        self.service.preview(staged["sessionId"], selections, config)
        self.assertEqual(json.dumps(self.service.templates(), ensure_ascii=False, sort_keys=True), before)



if __name__ == "__main__":
    unittest.main(verbosity=2)
