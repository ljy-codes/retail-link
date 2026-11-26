"""Pure tests for the analysis coverage report."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.data_health import build_health_report


class DataHealthTests(unittest.TestCase):
    def snapshot(self, **changes):
        base = {
            "id": "snapshot-1",
            "filename": "经营样例.xlsx",
            "period": "2026-11",
            "asOf": "2026-11-03",
            "sourceInfo": {"path": "/tmp/经营样例.xlsx", "sha256": "a" * 64},
            "sources": [
                {"role": "summary", "sheet": "月计划", "records": 1,
                 "startDate": "2026-11-01", "endDate": "2026-11-01"},
                {"role": "sales", "sheet": "平台销量", "records": 2,
                 "startDate": "2026-11-01", "endDate": "2026-11-03"},
                {"role": "loss", "sheet": "货损", "records": 0,
                 "startDate": None, "endDate": None},
            ],
            "trend": [{"date": "2026-11-01"}, {"date": "2026-11-03"}],
            "products": [{"id": "P1", "plan": 100, "completed": 90,
                          "stockSmall": 30, "salesQuantity": 10,
                          "outboundQuantity": 10, "lossAmount": None,
                          "warehouseStocks": [{"warehouseId": "W1", "warehouse": "南京"}],
                          "warehouse": "南京"}],
            "summary": {"achievement": 0.9, "stockAmount": 300,
                        "lossRate": None, "knownTargetGap": 10},
            "issues": [{"severity": "warning", "title": "日期待核对",
                        "detail": "来源期间不完整", "code": "DATE_GAP"}],
            "operationsSnapshot": {
                "complete": False,
                "coverage": {"status": {"shipments": False, "aftersales": False},
                             "declarations": []},
                "issues": [{"code": "COVERAGE_INCOMPLETE", "message": "缺少覆盖声明"}],
                "items": [{"product": "P1"}],
            },
        }
        base.update(changes)
        return base

    def test_report_preserves_unknowns_and_exposes_date_gaps(self):
        snapshot = self.snapshot()
        report = build_health_report(snapshot)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["scope"]["warehouses"], ["W1", "南京"])
        self.assertEqual(report["scope"]["observedDates"], ["2026-11-01", "2026-11-03"])
        self.assertIn("2026-11-02", report["scope"]["dateGaps"])
        self.assertEqual(report["missing"]["lossAmount"]["status"], "missing")
        net = next(item for item in report["metrics"] if item["key"] == "netSales")
        self.assertEqual(net["status"], "partial")
        self.assertTrue(any(item["key"] == "operations_facts" for item in report["nextActions"]))
        self.assertEqual(snapshot["products"][0]["lossAmount"], None)

    def test_complete_operation_coverage_can_mark_net_sales_available(self):
        snapshot = self.snapshot(
            operationsSnapshot={
                "complete": True,
                "coverage": {"status": {"shipments": True, "aftersales": True},
                             "declarations": [{"kind": "shipments"}]},
                "issues": [], "items": [{"product": "P1"}],
            },
            issues=[],
        )
        report = build_health_report(snapshot)
        net = next(item for item in report["metrics"] if item["key"] == "netSales")
        self.assertEqual(net["status"], "available")
        self.assertEqual(report["coverage"]["operations"]["shipments"], True)
        self.assertEqual(report["issueCount"], 0)

    def test_old_snapshot_without_sources_remains_json_safe(self):
        report = build_health_report({"id": "legacy", "products": [], "summary": {}})
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["source"]["files"], [])
        self.assertIsNone(report["scope"]["start"])
        self.assertGreaterEqual(len(report["nextActions"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

