"""Per-product target assessment through real workbook analysis and Excel output.

All products and amounts are fictional. Fixtures and generated outputs live in
disposable folders beside this test; no customer workbook is read or rewritten.
"""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

import openpyxl


ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT
TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DEV))
analyzer = importlib.import_module("backend.analyzer")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def find_product(result, identity):
    return next(item for item in result["products"] if item["id"] == identity)


class FictionalAssessmentFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="per-product-assessment-", dir=TEST_DIR)
        self.folder = Path(self.temp.name)
        self.serial = 0

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self, products, *, facts=None, as_of="2026-11-20", period="2026-11", target=.85):
        """products: [(stable ID, plan, completed)] with None genuinely missing."""
        self.serial += 1
        path = self.folder / f"虚构商品考核-{self.serial}.xlsx"
        workbook = openpyxl.Workbook()
        master = workbook.active
        master.title = "商品主档"
        master.append(["商品标识", "编码", "名称", "箱规", "大单位到岸价", "小单位到岸价"])
        plans = workbook.create_sheet("本月计划")
        plans.append(["商品标识", "商品名称", "计划金额"])
        orders = workbook.create_sheet("日期报单")
        orders.append(["商品标识", "业务日期", "数量", "单价"])
        for index, (identity, plan, completed) in enumerate(products, 1):
            master.append([identity, f"{index:05d}", f"虚构商品{identity}", 1, 1, 1])
            plans.append([identity, f"虚构商品{identity}", plan])
            if facts is None and completed is not None:
                orders.append([identity, as_of, completed, 1])
        if facts is not None:
            for identity, day, quantity in facts:
                orders.append([identity, day, quantity, 1])
        workbook.save(path)
        workbook.close()
        mappings = {role: {"sheet": "", "headerRow": 1, "columns": {}}
                    for role in ("summary", "products", "conversion", "inventory", "sales", "outbound", "orders", "transit", "loss")}
        mappings.update({
            "summary": {"sheet": "本月计划", "headerRow": 1,
                        "columns": {"product": "A", "name": "B", "plan": "C"}},
            "products": {"sheet": "商品主档", "headerRow": 1,
                         "columns": {"product": "A", "code": "B", "name": "C", "pack": "D", "basePriceLarge": "E", "basePrice": "F"}},
            "orders": {"sheet": "日期报单", "headerRow": 1,
                       "columns": {"product": "A", "date": "B", "quantity": "C", "basePrice": "D"}},
        })
        config = {"asOf": as_of, "period": period, "targetRate": target,
                  "amountMode": "calculated", "planBasis": "orders_amount", "averageDays": 1,
                  "inventoryComparable": False, "mappings": mappings, "overrides": {}}
        return path, config

    def analyze(self, products=None, **kwargs):
        path, config = self.fixture(products or [("A", 100, 200), ("B", 100, 0)], **kwargs)
        before = sha256(path)
        result = analyzer.analyze_workbook(path, config)
        self.assertEqual(sha256(path), before, "Analysis must not edit the source workbook")
        # Fails on NaN/Infinity as well as non-JSON output.
        json.dumps(result, ensure_ascii=False, allow_nan=False)
        return result


class PerProductAssessmentTests(FictionalAssessmentFixture):
    def test_total_100_percent_does_not_hide_an_unmet_product(self):
        result = self.analyze()
        summary = result["summary"]
        self.assertEqual(summary["plan"], 200)
        self.assertEqual(summary["completed"], 200)
        self.assertEqual(summary["achievement"], 1)
        self.assertEqual(summary["assessmentScope"], "product")
        self.assertEqual(summary["assessmentVersion"], 1)
        self.assertEqual(summary["metCount"], 1)
        self.assertEqual(summary["belowTargetCount"], 1)
        self.assertEqual(summary["unknownTargetCount"], 0)
        self.assertEqual(summary["targetGap"], 85)
        self.assertEqual(summary["knownTargetGap"], 85)
        self.assertEqual(summary["remainingDays"], 10)
        self.assertEqual(summary["dailyRequired"], 8.5)
        a, b = find_product(result, "A"), find_product(result, "B")
        self.assertEqual((a["assessmentStatus"], a["targetAmount"], a["targetGap"], a["dailyRequired"]), ("met", 85, 0, 0))
        self.assertEqual((b["assessmentStatus"], b["targetAmount"], b["targetGap"], b["dailyRequired"]), ("below", 85, 85, 8.5))

    def test_exactly_85_percent_counts_as_met(self):
        result = self.analyze([("A", 100, 85)])
        row = result["products"][0]
        self.assertEqual(row["assessmentStatus"], "met")
        self.assertEqual(row["targetGap"], 0)
        self.assertEqual(row["dailyRequired"], 0)
        self.assertEqual(result["summary"]["metCount"], 1)

    def test_large_plan_does_not_treat_a_real_cent_shortfall_as_roundoff(self):
        result = self.analyze([("A", 1_000_000_000_000, 849_999_999_999.99)])
        row = result["products"][0]
        self.assertEqual(row["assessmentStatus"], "below")
        self.assertGreater(row["targetGap"], .009)
        self.assertLess(row["targetGap"], .011)
        self.assertEqual(result["summary"]["belowTargetCount"], 1)

    def test_all_met_has_real_zero_gap_and_zero_daily_task(self):
        summary = self.analyze([("A", 100, 85), ("B", 200, 300)])["summary"]
        self.assertEqual((summary["metCount"], summary["belowTargetCount"], summary["unknownTargetCount"]), (2, 0, 0))
        self.assertEqual(summary["targetGap"], 0)
        self.assertEqual(summary["knownTargetGap"], 0)
        self.assertEqual(summary["dailyRequired"], 0)

    def test_zero_completed_is_a_real_unmet_result(self):
        result = self.analyze([("A", 100, 0)])
        row = result["products"][0]
        self.assertEqual(row["completed"], 0)
        self.assertEqual(row["assessmentStatus"], "below")
        self.assertEqual(row["targetGap"], 85)
        self.assertEqual(result["summary"]["unknownTargetCount"], 0)

    def test_missing_completed_is_unknown_and_does_not_erase_known_gap(self):
        result = self.analyze([("A", 100, 0), ("B", 100, None)])
        row, summary = find_product(result, "B"), result["summary"]
        self.assertIsNone(row["completed"])
        self.assertEqual(row["assessmentStatus"], "unknown")
        self.assertEqual(row["assessmentReasonCode"], "missing_completed")
        self.assertIsNone(row["targetGap"])
        self.assertIsNone(row["dailyRequired"])
        self.assertEqual(summary["knownTargetGap"], 85)
        self.assertIsNone(summary["targetGap"])
        self.assertIsNone(summary["dailyRequired"])
        self.assertEqual(summary["knownDailyRequired"], 8.5)
        self.assertEqual(summary["unknownTargetCount"], 1)

    def test_missing_plan_is_unknown_even_when_completed_is_high(self):
        result = self.analyze([("A", None, 1000), ("B", 100, 0)])
        row, summary = find_product(result, "A"), result["summary"]
        self.assertEqual(row["assessmentStatus"], "unknown")
        self.assertEqual(row["assessmentReasonCode"], "missing_plan")
        self.assertIsNone(row["targetAmount"])
        self.assertIsNone(row["targetGap"])
        self.assertIsNone(summary["targetGap"])
        self.assertEqual(summary["knownTargetGap"], 85)

    def test_nonpositive_plan_cannot_look_like_a_met_product(self):
        for plan in (0, -100):
            with self.subTest(plan=plan):
                row = self.analyze([("A", plan, 200)])["products"][0]
                self.assertEqual(row["assessmentStatus"], "unknown")
                self.assertEqual(row["assessmentReasonCode"], "nonpositive_plan")
                self.assertIsNone(row["targetAmount"])
                self.assertIsNone(row["targetGap"])
                self.assertIsNone(row["dailyRequired"])

    def test_all_unknown_is_missing_not_a_healthy_zero(self):
        summary = self.analyze([("A", None, None), ("B", 0, 0)])["summary"]
        self.assertEqual(summary["unknownTargetCount"], 2)
        self.assertEqual(summary["metCount"], 0)
        self.assertEqual(summary["belowTargetCount"], 0)
        self.assertIsNone(summary["knownTargetGap"])
        self.assertIsNone(summary["targetGap"])
        self.assertIsNone(summary["dailyRequired"])

    def test_known_zero_is_explicitly_partial_when_other_product_unknown(self):
        summary = self.analyze([("A", 100, 1000), ("B", None, 0)])["summary"]
        self.assertEqual(summary["metCount"], 1)
        self.assertEqual(summary["unknownTargetCount"], 1)
        self.assertEqual(summary["knownTargetGap"], 0)
        self.assertIsNone(summary["targetGap"])
        self.assertIsNone(summary["dailyRequired"])

    def test_month_end_unmet_is_overdue_not_division_by_zero(self):
        result = self.analyze(as_of="2026-11-30")
        a, b, summary = find_product(result, "A"), find_product(result, "B"), result["summary"]
        self.assertEqual(summary["remainingDays"], 0)
        self.assertEqual(summary["targetGap"], 85)
        self.assertIsNone(summary["dailyRequired"])
        self.assertEqual(b["assessmentStatus"], "below")
        self.assertEqual(b["assessmentReasonCode"], "period_ended")
        self.assertIsNone(b["dailyRequired"])
        self.assertEqual(a["assessmentStatus"], "met")
        self.assertEqual(a["dailyRequired"], 0)

    def test_cutoff_after_month_uses_month_end_and_excludes_next_month_records(self):
        result = self.analyze([("A", 100, None)], as_of="2026-12-10",
                              facts=[("A", "2026-11-30", 10), ("A", "2026-12-01", 1000)])
        row = result["products"][0]
        self.assertEqual(row["completed"], 10)
        self.assertEqual(row["targetGap"], 75)
        self.assertEqual(row["remainingDays"], 0)
        self.assertIsNone(row["dailyRequired"])

    def test_asof_excludes_future_reported_amount_from_target(self):
        result = self.analyze([("A", 100, None)],
                              facts=[("A", "2026-11-20", 0), ("A", "2026-11-21", 200)])
        self.assertEqual(result["products"][0]["completed"], 0)
        self.assertEqual(result["products"][0]["assessmentStatus"], "below")
        self.assertEqual(result["summary"]["targetGap"], 85)

    def test_earlier_month_and_empty_current_period_are_not_counted_as_zero(self):
        result = self.analyze([("A", 100, None)], period="2026-12", as_of="2026-12-01",
                              facts=[("A", "2026-11-30", 200)])
        row = result["products"][0]
        self.assertIsNone(row["completed"])
        self.assertEqual(row["assessmentStatus"], "unknown")
        self.assertEqual(result["summary"]["remainingDays"], 30)

    def test_asof_before_reporting_month_is_rejected(self):
        path, config = self.fixture([("A", 100, 0)], as_of="2026-10-31")
        with self.assertRaisesRegex(ValueError, "早于统计月份"):
            analyzer.analyze_workbook(path, config)

    def test_manual_plan_change_updates_status_and_can_be_removed(self):
        path, config = self.fixture([("A", 100, 200), ("B", 100, 80)])
        before_hash = sha256(path)
        original = analyzer.analyze_workbook(path, config)
        self.assertEqual(original["summary"]["targetGap"], 5)
        changed_config = copy.deepcopy(config)
        changed_config["overrides"] = {"B": {"plan": 50}}
        changed = analyzer.analyze_workbook(path, changed_config)
        self.assertEqual(find_product(changed, "B")["assessmentStatus"], "met")
        self.assertEqual(find_product(changed, "B")["targetAmount"], 42.5)
        self.assertEqual(changed["summary"]["targetGap"], 0)
        restored = analyzer.analyze_workbook(path, config)
        self.assertEqual(restored["summary"]["targetGap"], 5)
        self.assertEqual(sha256(path), before_hash)

    def test_configurable_80_percent_target_is_still_independent_per_product(self):
        result = self.analyze([("A", 100, 200), ("B", 100, 79)], target=.8)
        self.assertEqual(result["summary"]["targetRate"], .8)
        self.assertEqual(result["summary"]["targetGap"], 1)
        self.assertEqual(find_product(result, "A")["targetAmount"], 80)
        self.assertEqual(find_product(result, "B")["targetAmount"], 80)
        self.assertEqual(find_product(result, "B")["assessmentStatus"], "below")

    def test_floating_arithmetic_noise_does_not_make_false_unmet_targets(self):
        result = self.analyze([("A", .1, .08499999999999999)])
        self.assertEqual(result["products"][0]["assessmentStatus"], "met")
        self.assertEqual(result["summary"]["targetGap"], 0)

    def test_a_material_shortfall_is_not_rounded_into_met(self):
        result = self.analyze([("A", 100, 84.999)])
        self.assertEqual(result["products"][0]["assessmentStatus"], "below")
        self.assertAlmostEqual(result["summary"]["targetGap"], .001)

    def test_business_target_status_does_not_become_a_data_error(self):
        row = self.analyze([("A", 100, 0)])["products"][0]
        self.assertEqual(row["assessmentStatus"], "below")
        self.assertNotEqual(row["status"], "error")
        self.assertTrue(row["assessmentReason"])


class AssessmentExcelTests(FictionalAssessmentFixture):
    def workbook_result(self, products=None):
        result = self.analyze(products)
        result.update(id="fictional-assessment-snapshot", generatedAt="2026-11-20T00:00:00Z", appVersion="0.1.7")
        return result

    def export(self, result, filename):
        module = importlib.import_module("backend.export_service")
        service = module.ExportService(self.folder / "app-data", "0.1.7")
        output = self.folder / "outputs"
        output.mkdir(exist_ok=True)
        receipt = service.create_xlsx(result, output, filename)
        self.assertEqual(sha256(receipt["path"]), receipt["sha256"])
        return openpyxl.load_workbook(receipt["path"], data_only=True)

    def metric(self, sheet, *labels):
        matches = [(row[0].value, row[1].value, row[2].value) for row in sheet if row[0].value in labels]
        self.assertEqual(len(matches), 1, f"Expected one metric {labels}, got {matches}")
        return matches[0][1]

    def header_column(self, sheet, *labels):
        matches = [cell.column for cell in sheet[1] if cell.value in labels]
        self.assertEqual(len(matches), 1, f"Expected one header {labels}")
        return matches[0]

    def test_exported_total_and_each_product_do_not_offset_shortfalls(self):
        result = self.workbook_result()
        with __import__("contextlib").closing(self.export(result, "逐款考核.xlsx")) as workbook:
            overview = workbook["看盘总览"]
            self.assertEqual(self.metric(overview, "逐款缺口合计"), 85)
            self.assertEqual(self.metric(overview, "剩余每日需完成"), 8.5)
            self.assertEqual(self.metric(overview, "已达标商品"), 1)
            self.assertEqual(self.metric(overview, "未达标商品"), 1)
            self.assertEqual(self.metric(overview, "待核对商品"), 0)
            detail = workbook["商品明细"]
            target_col = self.header_column(detail, "考核目标完成额")
            status_col = self.header_column(detail, "单品考核")
            gap_col = self.header_column(detail, "距目标差额", "单品目标缺口", "目标缺口")
            task_col = self.header_column(detail, "剩余每日需完成", "剩余每日任务")
            identity_col = self.header_column(detail, "商品标识")
            rows = {detail.cell(row, identity_col).value: row for row in range(2, detail.max_row + 1)}
            a, b = rows["A"], rows["B"]
            self.assertEqual(detail.cell(a, target_col).value, 85)
            self.assertEqual(detail.cell(b, target_col).value, 85)
            self.assertEqual(detail.cell(a, status_col).value, "已达标")
            self.assertEqual(detail.cell(b, status_col).value, "未达标")
            self.assertEqual(detail.cell(a, gap_col).value, 0)
            self.assertEqual(detail.cell(b, gap_col).value, 85)
            self.assertEqual(detail.cell(b, task_col).value, 8.5)

    def test_unknown_target_export_keeps_total_blank_and_known_subtotal_explicit(self):
        result = self.workbook_result([("A", 100, 0), ("B", 100, None)])
        with __import__("contextlib").closing(self.export(result, "部分缺失.xlsx")) as workbook:
            overview = workbook["看盘总览"]
            self.assertIsNone(self.metric(overview, "逐款缺口合计"))
            self.assertEqual(self.metric(overview, "已知商品缺口"), 85)
            self.assertEqual(self.metric(overview, "待核对商品"), 1)
            detail = workbook["商品明细"]
            task_col = self.header_column(detail, "剩余每日需完成", "剩余每日任务")
            status_col = self.header_column(detail, "单品考核")
            self.assertEqual(detail.cell(3, status_col).value, "待核对")
            self.assertIsNone(detail.cell(3, task_col).value)

    def test_legacy_snapshot_export_preserves_old_value_and_identifies_old_scope(self):
        legacy = self.workbook_result()
        for key in ("assessmentScope", "assessmentVersion", "metCount", "belowTargetCount", "unknownTargetCount", "knownTargetGap", "knownDailyRequired", "dailyRequired", "remainingDays"):
            legacy["summary"].pop(key, None)
        legacy["summary"]["targetGap"] = 0
        legacy["appVersion"] = "0.1.5"
        for product in legacy["products"]:
            for key in ("assessmentStatus", "assessmentReason", "assessmentReasonCode", "targetAmount", "remainingDays"):
                product.pop(key, None)
        before = json.dumps(legacy, ensure_ascii=False, sort_keys=True)
        with __import__("contextlib").closing(self.export(legacy, "旧版快照.xlsx")) as workbook:
            overview = workbook["看盘总览"]
            text = "\n".join(str(cell.value) for row in overview for cell in row if cell.value is not None)
            self.assertIn("历史", text)
            self.assertTrue("旧口径" in text or "旧版口径" in text or "历史口径" in text or "历史记录口径" in text)
            self.assertEqual(self.metric(overview, "距目标差额", "历史距目标差额"), 0)
            if any(row[0].value == "逐款缺口合计" for row in overview):
                self.assertIsNone(self.metric(overview, "逐款缺口合计"))
        self.assertEqual(json.dumps(legacy, ensure_ascii=False, sort_keys=True), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
