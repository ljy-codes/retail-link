"""Independent integrity regressions. Uses only disposable synthetic workbooks."""
from datetime import datetime
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT
TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DEV))
from backend.operations import OperationsStore
from backend.source_writeback import SourceWriteback, sha
from backend import source_writeback as writeback_module


class IntegrityTests(unittest.TestCase):
    def test_loss_unit_price_denominator_cannot_disagree_with_quantity(self):
        from backend.operations_import import normalize_record
        row = {"product": "A", "warehouse": "甲仓", "date": "2026-11-10", "unit": "箱",
               "registered": 3, "opening": 100, "inbound": 50, "outbound": 120, "physical": 25,
               "unitPrice": "7.7元/件"}
        with self.assertRaisesRegex(ValueError, "单位"):
            normalize_record(row, "loss")
        row["unitPrice"] = "7.7元/箱"
        parsed = normalize_record(row, "loss")
        self.assertEqual(parsed["unitPrice"], 7.7)
        self.assertEqual(parsed["unitPriceBasis"], "元/箱")
        parsed["quantityUnits"]["opening"] = "件"
        with self.assertRaisesRegex(ValueError, "单位"):
            normalize_record(parsed, "loss")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="v02-integrity-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / "plain.xlsx"
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "业务"
        sheet.append(["商品", "规格", "月计划", "辅助公式"])
        sheet.append(["A", "1件", 10, "=C2*2"])
        book.save(self.path)
        book.close()
        self.writer = SourceWriteback(self.root / "runtime")
        self.writer.settings(True)
        self.store = OperationsStore(self.root / "operations-data")

    def entry(self):
        return {"path": str(self.path), "sha256": sha(self.path)}

    def template(self):
        return self.writer.register_template(self.entry(), {"sheet": "业务", "keyColumns": {"product": "A", "specification": "B"}, "fields": {"plan": "C"}})["template"]

    def preview(self, template):
        return self.writer.preview(self.entry(), {"templateId": template["id"], "changes": [{"keys": {"product": "A", "specification": "1件"}, "fields": {"plan": 20}}]})

    def save(self, kind, records, coverage=None):
        preview = self.store.preview_records({"kind": kind, "records": records, **({"coverage": coverage} if coverage else {})})
        return self.store.commit_import(preview)

    def shipment(self, **fields):
        return {"businessId": "S1", "product": "A", "warehouseId": "W1", "warehouse": "甲仓", "sourceSystem": "SYS-A", "date": "2026-11-10", "originalDate": "2026-11-10", "statusDate": "2026-11-10", "quantity": 10, "unit": "件", "baseUnit": "件", "status": "已发货", **fields}

    def aftersale(self, **fields):
        return {"businessId": "A1", "shipmentId": "S1", "product": "A", "warehouseId": "W1", "warehouse": "甲仓", "sourceSystem": "SYS-A", "date": "2026-11-15", "statusDate": "2026-11-15", "quantity": 2, "unit": "件", "baseUnit": "件", "type": "return", "status": "已完成", **fields}

    def coverage(self, **fields):
        return {"start": "2026-11-01", "end": "2026-12-31", "complete": True, "evidence": "仅临时合成范围", **fields}

    def test_preview_does_not_bind_old_rows_to_new_external_workbook(self):
        template = self.template()
        actual_rows = self.writer._rows
        modified = False
        def read_then_external_save(sheet, schema):
            nonlocal modified
            result = actual_rows(sheet, schema)
            if not modified:
                modified = True
                other = openpyxl.load_workbook(self.path)
                other["业务"]["A2"] = "B"
                other["业务"]["C2"] = 99
                other.save(self.path)
                other.close()
            return result
        try:
            with patch.object(self.writer, "_rows", side_effect=read_then_external_save):
                preview = self.preview(template)
            self.writer.commit(preview["token"], True)
        except ValueError:
            pass
        check = openpyxl.load_workbook(self.path)
        try:
            self.assertEqual((check["业务"]["A2"].value, check["业务"]["C2"].value), ("B", 99), "外部换行后不得把A的修正写到B")
        finally:
            check.close()

    def test_future_cancelled_state_does_not_erase_known_historical_shipment(self):
        self.save("shipments", [self.shipment()])
        self.save("shipments", [self.shipment(status="已取消", statusDate="2026-12-01")])
        report = self.store.reconcile("2026-11-01", "2026-11-30", as_of="2026-11-30")
        observed = sum(row["shipmentQuantity"] for row in report["items"])
        self.assertEqual(observed, 10, "历史查询应使用截至观察日已知的有效版本")

    def test_future_closed_aftersale_does_not_erase_known_historical_deduction(self):
        self.save("shipments", [self.shipment()])
        self.save("aftersales", [self.aftersale()])
        self.save("aftersales", [self.aftersale(status="已关闭", statusDate="2026-12-01")])
        report = self.store.reconcile("2026-11-01", "2026-11-30", as_of="2026-11-30")
        observed = sum(row["occurrenceDeduction"] for row in report["items"])
        self.assertEqual(observed, 2, "未来关闭状态不能回写历史已完成扣减")

    def test_coverage_from_other_source_product_and_warehouse_does_not_complete_a(self):
        try:
            self.save("shipments", [self.shipment()], self.coverage(scope=[{"sourceSystem": "SYS-A", "product": "A", "warehouseId": "W1", "warehouse": "甲仓", "sku": "", "specification": "", "shop": ""}]))
            self.save("aftersales", [], self.coverage(zeroRecordsConfirmed=True, scope=[{"sourceSystem": "SYS-B", "product": "B", "warehouseId": "W2", "warehouse": "乙仓", "sku": "", "specification": "", "shop": ""}]))
        except ValueError:
            # Refusing unsupported coverage scopes is safe; ignoring them is not.
            return
        report = self.store.reconcile("2026-11-01", "2026-11-30")
        self.assertFalse(report["complete"])
        self.assertTrue(all(row["netQuantity"] is None for row in report["items"]))

    def test_changed_state_without_effective_date_cannot_claim_a_complete_historical_net(self):
        self.save("shipments", [self.shipment(), self.shipment(businessId="S2", quantity=5)], self.coverage())
        scope = [{"sourceSystem": "SYS-A", "product": "A", "warehouseId": "W1", "warehouse": "甲仓", "sku": "", "specification": "", "shop": ""}]
        self.save("aftersales", [], self.coverage(zeroRecordsConfirmed=True, scope=scope))
        self.save("shipments", [self.shipment(status="已取消", statusDate=None)])
        report = self.store.reconcile("2026-11-01", "2026-11-15", as_of="2026-11-15")
        self.assertFalse(report["complete"], "缺生效日期的取消修订不能静默回填历史")
        self.assertTrue(all(row["netQuantity"] is None for row in report["items"]))

    def test_same_business_id_in_distinct_sources_links_within_its_source(self):
        self.save("shipments", [self.shipment(), self.shipment(sourceSystem="SYS-B", quantity=20)])
        self.save("aftersales", [self.aftersale(sourceSystem="SYS-B")])
        report = self.store.reconcile("2026-11-01", "2026-11-30")
        self.assertEqual(sorted(row["observedNetQuantity"] for row in report["items"] if row["shipmentQuantity"]), [10, 18])

    def test_mapped_sku_needs_coverage_for_every_contributing_source(self):
        scope = lambda system: {"sourceSystem": system, "product": "A", "warehouseId": "W1", "warehouse": "甲仓", "sku": "", "specification": "", "shop": ""}
        try:
            self.save("shipments", [self.shipment(), self.shipment(businessId="S2", sourceSystem="SYS-B", quantity=20)], self.coverage())
            self.save("aftersales", [], self.coverage(zeroRecordsConfirmed=True, scope=[scope("SYS-A")]))
        except ValueError:
            return
        catalog = [{"sourceSystem": system, "sourceProduct": "A", "sourceShop": "甲仓", "sourceWarehouseId": "W1", "product": "STD", "warehouseId": "W1", "shop": "甲仓"} for system in ("SYS-A", "SYS-B")]
        report = self.store.reconcile("2026-11-01", "2026-11-30", catalog=catalog)
        self.assertEqual(len(report["items"]), 1)
        self.assertFalse(report["items"][0]["complete"])
        self.assertIsNone(report["items"][0]["netQuantity"])

    def test_process_crash_after_publish_can_restore_from_prepared_record(self):
        template = self.template()
        before = self.path.read_bytes()
        preview = self.preview(template)
        actual_json = writeback_module._atomic_json
        class SimulatedCrash(BaseException):
            pass
        def crash_before_success_record(path, value):
            if value.get("status") == "success":
                raise SimulatedCrash()
            return actual_json(path, value)
        with patch.object(writeback_module, "_atomic_json", side_effect=crash_before_success_record):
            with self.assertRaises(SimulatedCrash):
                self.writer.commit(preview["token"], True)
        restarted = SourceWriteback(self.root / "runtime")
        record = restarted.history()["items"][0]
        self.assertEqual(record["status"], "prepared")
        restarted.restore(record["id"], True)
        self.assertEqual(self.path.read_bytes(), before)

    def test_loss_never_compares_box_registration_with_piece_stock_movements(self):
        for declared in ("", "件"):
            with self.subTest(declaredUnit=declared):
                isolated = OperationsStore(self.root / ("mixed-loss-" + (declared or "unknown")))
                row = {"product": "A", "warehouseId": "W1", "warehouse": "甲仓", "date": "2026-11-15", "unit": declared,
                       "registered": "2箱", "opening": "100件", "inbound": "50件", "outbound": "120件", "physical": "25件"}
                try:
                    preview = isolated.preview_records({"kind": "loss", "records": [row]})
                    isolated.commit_import(preview)
                except ValueError:
                    self.assertEqual(isolated.list("loss")["total"], 0)
                    continue
                result = isolated.loss_report()["items"][0]
                self.assertIsNone(result["rebuilt"], "单位冲突的期初/流水/实盘不得直接相加或与登记箱数比较")
                self.assertIsNone(result["difference"])

    def test_macro_payload_under_xlsx_extension_is_not_silently_removed(self):
        with zipfile.ZipFile(self.path, "a") as package:
            package.writestr("xl/vbaProject.bin", b"synthetic-vba-marker")
        before = self.path.read_bytes()
        try:
            template = self.template()
            self.writer.commit(self.preview(template)["token"], True)
        except ValueError:
            self.assertEqual(self.path.read_bytes(), before)
            return
        with zipfile.ZipFile(self.path) as package:
            self.assertIn("xl/vbaProject.bin", package.namelist(), "不能静默删除原件中的宏或其他不支持内容")

    def test_unsupported_validation_extension_is_preserved_or_source_is_refused(self):
        with zipfile.ZipFile(self.path) as package:
            parts = {name: package.read(name) for name in package.namelist()}
        main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        x14 = "http://schemas.microsoft.com/office/spreadsheetml/2009/9/main"
        xm = "http://schemas.microsoft.com/office/excel/2006/main"
        tree = ET.fromstring(parts["xl/worksheets/sheet1.xml"])
        extensions = ET.SubElement(tree, "{" + main + "}extLst")
        extension = ET.SubElement(extensions, "{" + main + "}ext", {"uri": "{CCE6A557-97BC-4b89-ADB6-D9C93CAAB3DF}"})
        validations = ET.SubElement(extension, "{" + x14 + "}dataValidations", {"count": "1"})
        validation = ET.SubElement(validations, "{" + x14 + "}dataValidation", {"type": "whole", "operator": "between"})
        for label, number in (("formula1", "0"), ("formula2", "100")):
            formula = ET.SubElement(validation, "{" + x14 + "}" + label)
            ET.SubElement(formula, "{" + xm + "}f").text = number
        ET.SubElement(validation, "{" + xm + "}sqref").text = "C2"
        parts["xl/worksheets/sheet1.xml"] = ET.tostring(tree)
        with zipfile.ZipFile(self.path, "w", zipfile.ZIP_DEFLATED) as package:
            for name, data in parts.items():
                package.writestr(name, data)
        before = self.path.read_bytes()
        try:
            template = self.template()
            self.writer.commit(self.preview(template)["token"], True)
        except ValueError:
            self.assertEqual(self.path.read_bytes(), before)
            return
        with zipfile.ZipFile(self.path) as package:
            saved = ET.fromstring(package.read("xl/worksheets/sheet1.xml"))
        self.assertIsNotNone(saved.find("{" + main + "}extLst"), "不能静默删除原表中的扩展数据验证规则")

    def test_untouched_formula_cache_is_preserved_or_source_is_refused(self):
        with zipfile.ZipFile(self.path) as package:
            parts = {name: package.read(name) for name in package.namelist()}
        ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        sheet = ET.fromstring(parts["xl/worksheets/sheet1.xml"])
        cached = sheet.find('.//m:c[@r="D2"]/m:v', ns)
        sheet.find('.//m:c[@r="D2"]/m:f', ns).text = "10*2"
        cached.text = "20"
        parts["xl/worksheets/sheet1.xml"] = ET.tostring(sheet)
        with zipfile.ZipFile(self.path, "w", zipfile.ZIP_DEFLATED) as package:
            for name, data in parts.items():
                package.writestr(name, data)
        check = openpyxl.load_workbook(self.path, data_only=True)
        self.assertEqual(check["业务"]["D2"].value, 20)
        check.close()
        before = self.path.read_bytes()
        try:
            template = self.template()
            self.writer.commit(self.preview(template)["token"], True)
        except ValueError:
            self.assertEqual(self.path.read_bytes(), before)
            return
        check = openpyxl.load_workbook(self.path, data_only=True)
        try:
            self.assertEqual(check["业务"]["D2"].value, 20, "非目标公式缓存不能因写回丢失，造成下一次只读分析为空")
        finally:
            check.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
