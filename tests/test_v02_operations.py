"""Synthetic behavior tests for reviewed v0.2 operational facts."""
from copy import deepcopy
import io
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
from backend.operations import OperationsStore, validate_state


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="operations-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = OperationsStore(self.root / "data")

    def save(self, kind, records, coverage=None, **options):
        body = {"kind": kind, "records": records, **options}
        if coverage:
            coverage = deepcopy(coverage)
            if not records and coverage.get("zeroRecordsConfirmed"):
                coverage.setdefault("scope", [{field: r.get(field, "") for field in
                    ("sourceSystem", "product", "sku", "specification", "warehouse", "warehouseId", "shop")}
                    for r in self.store.list("shipments")["items"]])
            body["coverage"] = coverage
        preview = self.store.preview_records(body)
        result = self.store.commit_import(preview)
        return preview, result

    def shipment(self, business="S1", **overrides):
        return {"businessId": business, "orderId": "O1", "lineId": business,
                "product": "000001", "warehouse": "合成甲仓", "date": "2026-10-30", "originalDate": "2026-10-30",
                "quantity": 10, "unit": "件", "baseUnit": "件", "status": "已发货", **overrides}

    def aftersale(self, business="A1", **overrides):
        return {"businessId": business, "shipmentId": "S1", "product": "000001", "warehouse": "合成甲仓",
                "date": "2026-11-02", "quantity": 2, "unit": "件", "baseUnit": "件",
                "type": "return", "status": "已完成", **overrides}

    def coverage(self, start="2026-10-01", end="2026-11-30", **overrides):
        return {"start": start, "end": end, "complete": True, "evidence": "合成来源的完整范围声明", **overrides}

    def test_preview_is_read_only_and_commit_requires_its_exact_fingerprint(self):
        preview = self.store.preview_records({"kind": "shipments", "records": [self.shipment()]})
        self.assertFalse(self.store.path.exists())
        with self.assertRaisesRegex(ValueError, "指纹"):
            self.store.commit_import(preview["token"], "tampered")
        self.assertEqual(self.store.list()["total"], 0)
        self.assertEqual(self.store.commit_import(preview)["saved"], 1)
        self.assertEqual(OperationsStore(self.root / "data").list()["total"], 1)

    def test_stale_preview_from_another_store_cannot_overwrite_new_data(self):
        preview = self.store.preview_records({"kind": "shipments", "records": [self.shipment()]})
        other = OperationsStore(self.root / "data")
        other.commit_import(other.preview_records({"kind": "shipments", "records": [self.shipment("S2")]}))
        with self.assertRaisesRegex(ValueError, "重新预览"):
            self.store.commit_import(preview)
        self.assertEqual(self.store.list()["items"][0]["businessId"], "S2")

    def test_repeat_is_unchanged_and_status_revision_keeps_the_previous_fact(self):
        self.save("shipments", [self.shipment(status="待发货")])
        preview, result = self.save("shipments", [self.shipment(status="待发货")])
        self.assertEqual((preview["reviewCounts"]["unchanged"], result["saved"]), (1, 0))
        self.save("shipments", [self.shipment(status="已发货")])
        current = self.store.list()["items"][0]
        self.assertEqual(current["revision"], 2)
        self.assertEqual(self.store.revisions(current["id"])["items"][0]["record"]["status"], "待发货")

    def test_conflicting_duplicate_ids_inside_one_import_are_not_silently_selected(self):
        preview = self.store.preview_records({"kind": "shipments", "records": [self.shipment(), self.shipment(quantity=12)]})
        self.assertEqual(preview["reviewCounts"]["blocked"], 1)
        self.assertEqual(preview["validCount"], 0)
        with self.assertRaisesRegex(ValueError, "没有可保存"):
            self.store.commit_import(preview)

    def test_different_order_lines_same_date_product_and_quantity_are_preserved(self):
        self.save("shipments", [self.shipment(), self.shipment("S2")])
        report = self.store.reconcile("2026-10-01", "2026-10-31")
        self.assertEqual(report["items"][0]["shipmentQuantity"], 20)

    def test_two_period_views_and_negative_occurrence_day_keep_original_period_separate(self):
        self.save("shipments", [self.shipment()], self.coverage())
        self.save("aftersales", [self.aftersale()], self.coverage())
        october = self.store.reconcile("2026-10-01", "2026-10-31", as_of="2026-11-30")
        self.assertTrue(october["complete"])
        self.assertEqual(october["items"][0]["originalNetQuantity"], 8)
        self.assertEqual(october["items"][0]["occurrenceNetQuantity"], 10)
        november = self.store.reconcile("2026-11-01", "2026-11-30")
        self.assertEqual(november["items"][0]["originalNetQuantity"], 0)
        self.assertEqual(november["items"][0]["occurrenceNetQuantity"], -2)
        self.assertEqual(november["items"][0]["daily"][0]["occurrenceObservedNetQuantity"], -2)

    def test_linked_return_and_refund_deduct_once_and_reimport_does_not_accumulate(self):
        self.save("shipments", [self.shipment()], self.coverage())
        rows = [self.aftersale(groupId="CASE1"), self.aftersale("A2", type="refund", groupId="CASE1")]
        self.save("aftersales", rows, self.coverage())
        self.save("aftersales", rows, self.coverage())
        report = self.store.reconcile("2026-10-01", "2026-11-30")
        self.assertEqual(report["items"][0]["netQuantity"], 8)
        self.assertEqual(len(report["details"]), 1)
        self.assertEqual(len(report["details"][0]["recordIds"]), 2)

    def test_unlinked_return_refund_overlap_needs_confirmation_not_double_deduction(self):
        self.save("shipments", [self.shipment()], self.coverage())
        self.save("aftersales", [self.aftersale(), self.aftersale("A2", type="refund")], self.coverage())
        report = self.store.reconcile("2026-10-01", "2026-11-30")
        self.assertFalse(report["complete"])
        self.assertIsNone(report["items"][0]["netQuantity"])
        self.assertTrue(any(i["code"] == "OVERLAP_PENDING" for i in report["issues"]))

    def test_distinct_partial_claims_add_but_unclear_overlap_quantities_do_not(self):
        self.save("shipments", [self.shipment()], self.coverage())
        self.save("aftersales", [self.aftersale(groupId="PART1", quantity=2),
                                  self.aftersale("A2", groupId="PART2", quantity=3)], self.coverage())
        self.assertEqual(self.store.reconcile("2026-10-01", "2026-11-30")["items"][0]["netQuantity"], 5)
        self.save("aftersales", [self.aftersale("A2", groupId="PART1", quantity=3, type="refund")])
        report = self.store.reconcile("2026-10-01", "2026-11-30")
        self.assertIsNone(report["items"][0]["netQuantity"])
        self.assertTrue(any(i["code"] == "GROUP_QUANTITY_PENDING" for i in report["issues"]))

    def test_cancel_without_shipped_base_does_not_reduce_other_shipment(self):
        self.save("shipments", [self.shipment(), self.shipment("S2", status="已取消")])
        self.save("aftersales", [self.aftersale(shipmentId="S2", type="cancel", quantity=10)])
        report = self.store.reconcile("2026-10-01", "2026-11-30")
        self.assertEqual(report["items"][0]["observedNetQuantity"], 10)
        self.assertEqual(report["details"][0]["decision"], "no_shipped_base")

    def test_unit_conversion_requires_explicit_units_and_box_basis(self):
        self.save("shipments", [self.shipment(quantity=1, unit="箱", baseUnit="件", pack=12)], self.coverage())
        self.save("aftersales", [self.aftersale()], self.coverage())
        self.assertEqual(self.store.reconcile("2026-10-01", "2026-11-30")["items"][0]["netQuantity"], 10)
        self.save("shipments", [self.shipment(quantity=1, unit="箱", baseUnit="件", pack=None)])
        report = self.store.reconcile("2026-10-01", "2026-11-30")
        self.assertFalse(report["complete"])
        self.assertTrue(any(i["code"] == "SHIPMENT_PENDING" for i in report["issues"]))

    def test_quantity_suffix_cannot_be_overridden_by_a_conflicting_declared_unit(self):
        preview = self.store.preview_records({"kind": "shipments", "records": [self.shipment(quantity="2箱", unit="件", baseUnit="件")]})
        self.assertEqual(preview["validCount"], 0)
        self.assertTrue(any("单位不一致" in issue["message"] for issue in preview["issues"]))
        self.assertEqual(self.store.list()["total"], 0)

    def test_loss_unit_suffixes_require_consistency_and_do_not_label_unknown_fields(self):
        row = {"product": "000001", "warehouse": "合成甲仓", "date": "2026-11-01", "registered": "2件",
               "opening": "100件", "inbound": "50件", "outbound": "120件", "physical": "25件"}
        self.save("loss", [row])
        result = self.store.loss_report()["items"][0]
        self.assertEqual((result["unit"], result["rebuilt"], result["difference"]), ("件", 5, 3))
        self.save("loss", [{**row, "product": "000002", "opening": 100}])
        result = next(r for r in self.store.loss_report()["items"] if r["product"] == "000002")
        self.assertIsNone(result["rebuilt"])
        self.assertEqual(result["quantityUnits"]["opening"], "")

    def test_excess_deduction_is_pending_not_silently_clipped(self):
        self.save("shipments", [self.shipment()], self.coverage())
        self.save("aftersales", [self.aftersale(quantity=12)], self.coverage())
        report = self.store.reconcile("2026-10-01", "2026-11-30")
        self.assertIsNone(report["items"][0]["netQuantity"])
        self.assertTrue(any(i["code"] == "OVER_DEDUCTION" for i in report["issues"]))
        self.assertEqual(self.store.list("aftersales")["items"][0]["quantity"], 12)

    def test_missing_refund_quantity_is_not_inferred_from_money(self):
        self.save("shipments", [self.shipment()], self.coverage())
        self.save("aftersales", [self.aftersale(quantity=None, amount=100)], self.coverage())
        report = self.store.reconcile("2026-10-01", "2026-11-30")
        self.assertIsNone(report["items"][0]["netQuantity"])
        self.assertTrue(any(i["code"] == "AFTERSALE_PENDING" for i in report["issues"]))

    def test_ad_hoc_coverage_toggle_does_not_claim_complete_net_sales(self):
        self.save("shipments", [self.shipment()])
        report = self.store.reconcile("2026-10-01", "2026-10-31", coverage={"shipmentsComplete": True, "aftersalesComplete": True})
        self.assertFalse(report["complete"])
        self.assertIsNone(report["items"][0]["netQuantity"])
        self.assertEqual(report["items"][0]["observedNetQuantity"], 10)

    def test_zero_aftersales_needs_explicit_source_range_and_zero_assertion(self):
        self.save("shipments", [self.shipment()], self.coverage())
        with self.assertRaisesRegex(ValueError, "0笔"):
            self.store.preview_records({"kind": "aftersales", "records": [], "coverage": self.coverage()})
        self.save("aftersales", [], self.coverage(zeroRecordsConfirmed=True))
        report = self.store.reconcile("2026-10-01", "2026-10-31")
        self.assertTrue(report["complete"])
        self.assertEqual(report["items"][0]["netQuantity"], 10)

    def test_distinct_original_sale_and_shipment_months_use_distinct_gross_bases(self):
        self.save("shipments", [self.shipment(date="2026-11-01", originalDate="2026-10-30")], self.coverage())
        self.save("aftersales", [self.aftersale(originalDate="2026-10-30")], self.coverage())
        october = self.store.reconcile("2026-10-01", "2026-10-31", as_of="2026-11-30")["items"][0]
        self.assertEqual((october["originalShipmentQuantity"], october["shipmentQuantity"]), (10, 0))
        self.assertEqual((october["originalNetQuantity"], october["occurrenceNetQuantity"]), (8, 0))

    def test_missing_original_sale_date_only_blocks_original_sale_view(self):
        self.save("shipments", [self.shipment(originalDate=None)], self.coverage())
        self.save("aftersales", [self.aftersale()], self.coverage())
        row = self.store.reconcile("2026-10-01", "2026-11-30")["items"][0]
        self.assertIsNone(row["originalNetQuantity"])
        self.assertEqual(row["occurrenceNetQuantity"], 8)
        self.assertFalse(row["originalComplete"])
        self.assertTrue(row["occurrenceComplete"])

    def test_two_source_skus_explicitly_mapped_to_one_standard_sku_can_link(self):
        self.save("shipments", [self.shipment(product="SRC_A", sku="A", specification="来源规格A")], self.coverage())
        self.save("aftersales", [self.aftersale(product="SRC_B", sku="B", specification="来源规格B")], self.coverage())
        catalog = [{"sourceProduct": product, "sourceSku": sku, "sourceSpecification": spec,
                    "sourceSystem": "", "sourceShop": "合成甲仓", "product": "STD_SKU", "name": "合成标准商品",
                    "specification": "统一规格", "shop": "合成标准仓", "warehouseId": "W1"}
                   for product, sku, spec in (("SRC_A", "A", "来源规格A"), ("SRC_B", "B", "来源规格B"))]
        report = self.store.reconcile("2026-10-01", "2026-11-30", catalog=catalog)
        self.assertEqual(len(report["items"]), 1)
        self.assertEqual((report["items"][0]["product"], report["items"][0]["netQuantity"]), ("STD_SKU", 8))

    def test_missing_warehouse_on_unmatched_aftersale_does_not_leave_shipment_falsely_complete(self):
        self.save("shipments", [self.shipment()], self.coverage())
        self.save("aftersales", [self.aftersale(shipmentId="unknown", warehouse="")], self.coverage())
        report = self.store.reconcile("2026-10-01", "2026-11-30")
        self.assertTrue(all(row["netQuantity"] is None for row in report["items"]))

    def test_status_effective_after_cutoff_is_not_retroactively_counted(self):
        self.save("shipments", [self.shipment(statusDate="2026-12-01")], self.coverage())
        report = self.store.reconcile("2026-10-01", "2026-11-30")
        self.assertFalse(report["complete"])
        self.assertTrue(any(i["code"] == "STATE_AFTER_CUTOFF" for i in report["issues"]))

    def test_loss_formula_missing_inputs_zero_and_registered_amount_stay_distinct(self):
        row = {"product": "000001", "warehouse": "合成甲仓", "date": "2026-11-01", "unit": "件",
               "registered": 2, "opening": 20, "inbound": 5, "outbound": 10, "physical": 13,
               "unitPrice": 7.7, "registeredAmount": 77}
        self.save("loss", [row])
        result = self.store.loss_report()["items"][0]
        self.assertEqual((result["rebuilt"], result["difference"], result["rebuiltAmount"], result["amountDifference"]), (2, 0, 15.4, -61.6))
        self.assertEqual((result["quantityStatus"], result["amountStatus"], result["status"]), ("consistent", "difference", "awaiting_confirmation"))
        self.save("loss", [{**row, "confirmationStatus": "confirmed", "confirmationNote": "合成说明：已核对原金额差异"}])
        self.assertEqual(self.store.loss_report()["items"][0]["status"], "confirmed_difference")
        self.save("loss", [{**row, "inbound": None}])
        result = self.store.loss_report()["items"][0]
        self.assertIsNone(result["rebuilt"])
        self.assertEqual(result["registeredAmount"], 77)
        self.assertEqual(result["status"], "uncomputed")

    def summary_workbook(self):
        path = self.root / "synthetic-20261130.xlsx"
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "仓库业务组"
        sheet.append(["商品信息", "仓库信息", "销售日期", "仓库总销售数", "销售数(份)", "商家报价"])
        sheet.append(["合成商品 商品ID:000009", "合成甲仓", "2026-11-02", 8, "4份", 3.5])
        sheet.append([None, None, None, None, None, 4.5])
        for col in "ABCDE":
            sheet.merge_cells(f"{col}2:{col}3")
        sheet = workbook.create_sheet("售后率")
        sheet.append(["商品信息", "商品规格", "销售数(件)", "售后数(件)", "品质退款数(件)", "售后率"])
        sheet.append(["000008+合成产品", "1件", 20, 4, 1, "20%"])
        sheet = workbook.create_sheet("货款")
        sheet.append(["销售日期", "商品ID", "商品规格", "商家报价（元）", "商品数量", "商品货款总额（元）", "先用后付服务费（元）"])
        sheet.append(["2026-11-01", "000007", "1组", 8, 3, 24, -0.6])
        workbook.save(path)
        workbook.close()
        return {"path": path, "filename": path.name}

    def test_realistic_summary_groups_preserve_units_fees_and_actual_coverage(self):
        entry = self.summary_workbook()
        before = entry["path"].read_bytes()
        preview = self.store.preview_import(entry, {"kind": "summary"})
        self.assertEqual(preview["validCount"], 3)
        self.assertEqual(preview["coverage"]["observedDates"], ["2026-11-01", "2026-11-02"])
        warehouse = next(r for r in preview["records"] if r["layout"] == "warehouse_summary")
        self.assertEqual(warehouse["metrics"]["销售数(份)"]["value"], 4)
        self.assertEqual(warehouse["metrics"]["销售数(份)"]["unit"], "份")
        self.assertEqual(len(warehouse["quotes"]), 2)
        settlement = next(r for r in preview["records"] if r["layout"] == "settlement")
        self.assertEqual(settlement["metrics"]["先用后付服务费（元）"]["value"], -0.6)
        self.assertEqual(settlement["quantityPriceDifference"], 0)
        aftersale = next(r for r in preview["records"] if r["layout"] == "aftersales_summary")
        self.assertIsNone(aftersale["date"])
        self.assertEqual(aftersale["product"], "000008")
        self.assertEqual(aftersale["ratioChecks"][0]["difference"], 0)
        self.assertTrue(any(i.get("code") == "MISSING_PERIOD" for i in preview["issues"]))
        self.store.commit_import(preview)
        self.assertEqual(entry["path"].read_bytes(), before)
        report = self.store.reconcile("2026-11-01", "2026-11-30")
        self.assertFalse(report["complete"])
        self.assertEqual(report["items"], [])

    def test_changed_source_bytes_invalidate_commit_without_writing_any_fact(self):
        entry = self.summary_workbook()
        preview = self.store.preview_import(entry, {"kind": "summary"})
        workbook = openpyxl.load_workbook(entry["path"])
        workbook["货款"]["E2"] = 10
        workbook.save(entry["path"])
        workbook.close()
        with self.assertRaisesRegex(ValueError, "原表已变化"):
            self.store.commit_import(preview)
        self.assertEqual(self.store.list()["total"], 0)

    def test_export_rebuilds_numbers_and_generation_instead_of_accepting_forged_report(self):
        self.save("shipments", [self.shipment()], self.coverage())
        self.save("aftersales", [], self.coverage(zeroRecordsConfirmed=True))
        report = self.store.reconcile("2026-10-01", "2026-10-31")
        report["items"][0]["netQuantity"] = 999999
        self.save("shipments", [self.shipment(quantity=12)])
        workbook = self.store.export_workbook(report)
        sheet = workbook["净销量核对"]
        headers = [c.value for c in sheet[1]]
        self.assertEqual(sheet.cell(2, headers.index("售后发生期净销量") + 1).value, 12)
        scope = workbook["口径说明"]
        self.assertEqual(dict(scope.iter_rows(min_row=2, values_only=True))["数据代次"], 3)
        self.assertTrue({"来源汇总", "发货事实", "售后事实", "货损双轨", "每日双视角", "待核对"}.issubset(workbook.sheetnames))
        workbook.close()

    def test_customer_export_has_flat_chinese_headers_and_keeps_negative_zero_missing_and_quotes(self):
        self.save("shipments", [self.shipment()], self.coverage())
        self.save("aftersales", [self.aftersale()], self.coverage())
        summary = self.store.preview_import(self.summary_workbook(), {"kind": "summary"})
        self.store.commit_import(summary)
        self.save("loss", [{"product": "000001", "warehouse": "合成甲仓", "date": "2026-11-01", "unit": "件",
                            "registered": 0, "opening": 0, "inbound": 0, "outbound": 0, "physical": 0, "unitPrice": 0, "registeredAmount": 0},
                           {"product": "000002", "warehouse": "合成甲仓", "date": "2026-11-01", "unit": "件",
                            "registered": 0, "opening": 0, "inbound": 0, "outbound": 0, "physical": None}])
        workbook = self.store.export_workbook({"start": "2026-11-01", "end": "2026-11-30"})
        self.assertEqual(workbook.sheetnames[0], "净销量核对")
        sheet = workbook["净销量核对"]
        headers = [c.value for c in sheet[1]]
        self.assertTrue({"商品标识", "原销售期净销量", "售后发生期净销量"}.issubset(headers))
        self.assertFalse({"daily", "sources", "coverageStatus", "source"}.intersection(headers))
        row = dict(zip(headers, [c.value for c in sheet[2]]))
        self.assertEqual((row["发生期发货量"], row["原销售期净销量"], row["售后发生期净销量"]), (0, 0, -2))
        self.assertTrue(all(not isinstance(cell.value, str) or not cell.value.startswith(("{", "[")) for cells in sheet for cell in cells))
        self.assertEqual((sheet.freeze_panes, sheet.auto_filter.ref, sheet["A1"].fill.fgColor.rgb[-6:]), ("A2", sheet.dimensions, "17354A"))
        summary_sheet = workbook["来源汇总"]
        summary_headers = [c.value for c in summary_sheet[1]]
        summary_rows = [dict(zip(summary_headers, values)) for values in summary_sheet.iter_rows(min_row=2, values_only=True)]
        warehouse = next(r for r in summary_rows if r["来源结构"] == "仓库业务组汇总")
        self.assertEqual((warehouse["商家报价1"], warehouse["商家报价2"], warehouse["原表·销售数(份)"]), (3.5, 4.5, 4))
        settlement = next(r for r in summary_rows if r["来源结构"] == "商家货款汇总")
        self.assertEqual(settlement["原表·先用后付服务费（元）"], -0.6)
        loss_sheet = workbook["货损双轨"]
        loss_headers = [c.value for c in loss_sheet[1]]
        loss_rows = {r["商品标识"]: r for r in (dict(zip(loss_headers, values)) for values in loss_sheet.iter_rows(min_row=2, values_only=True))}
        self.assertEqual(loss_rows["000001"]["公式盘差"], 0)
        self.assertIsNone(loss_rows["000002"]["公式盘差"])
        for other in ("每日双视角", "来源覆盖", "口径说明"):
            self.assertTrue(all(not isinstance(cell.value, str) or not cell.value.startswith(("{", "[")) for cells in workbook[other] for cell in cells))
        workbook.close()

    def test_configurable_event_mapping_preserves_long_text_ids_and_status_policy(self):
        path = self.root / "events.xlsx"
        workbook = openpyxl.Workbook()
        workbook.active.append(["单号", "编码", "时间", "个数", "履约"])
        workbook.active.append(["000000000000000001", "000001", "2026-11-01", 5, "源自定义状态"])
        workbook.save(path)
        workbook.close()
        preview = self.store.preview_import({"path": path}, {"kind": "shipments", "statusMap": {"源自定义状态": "effective"},
            "mapping": {"columns": {"businessId": "A", "product": "编码", "date": "C", "quantity": "D", "status": "E"},
                        "constants": {"unit": "件", "baseUnit": "件"}}})
        self.assertEqual(preview["records"][0]["businessId"], "000000000000000001")
        self.store.commit_import(preview)
        report = self.store.reconcile("2026-11-01", "2026-11-30")
        self.assertEqual(report["items"][0]["shipmentQuantity"], 5)
        exported = self.store.export_bytes()
        workbook = openpyxl.load_workbook(io.BytesIO(exported), data_only=False)
        sheet = workbook["业务事实"]
        headers = [c.value for c in sheet[1]]
        cell = sheet.cell(2, headers.index("业务明细ID") + 1)
        self.assertEqual((cell.value, cell.data_type, cell.number_format), ("000000000000000001", "s", "@"))
        workbook.close()

    def test_portable_state_validation_rejects_unknown_schema_nonfinite_and_forged_conversion(self):
        self.save("shipments", [self.shipment()])
        state = json.loads(self.store.path.read_text(encoding="utf-8"))
        self.assertIs(validate_state(state), state)
        for changed in [dict(state, unknown=True), dict(state, version=999), dict(state, generation=-1)]:
            with self.assertRaises(ValueError):
                validate_state(changed)
        changed = deepcopy(state)
        changed["records"][0]["normalizedQuantity"] = 999
        with self.assertRaises(ValueError):
            validate_state(changed)
        changed = deepcopy(state)
        changed["records"][0]["quantity"] = float("nan")
        with self.assertRaises(ValueError):
            validate_state(changed)


if __name__ == "__main__":
    unittest.main()
