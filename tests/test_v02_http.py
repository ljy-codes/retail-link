"""Isolated HTTP contracts for v0.2 reconciliation and explicit source writes.

All input rows are synthetic. Production paths and data are never used.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT
TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DEV))
spec = importlib.util.spec_from_file_location("v02_http_host", DEV / "server.py")
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


class QuietHandler(host.Handler):
    def log_message(self, *args):
        pass


class V02HttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="v02-http-")
        self.folder = Path(self.temporary.name)
        host.APP_DIR = self.folder / "app"
        host.APP_DIR.mkdir()
        host.PROJECT_DIR = host.APP_DIR
        host.DATA_DIR = self.folder / "runtime"
        host.ORIGINAL_DIR = self.folder / "synthetic-originals"
        host.ORIGINAL_DIR.mkdir()
        host.EXPORT_DIR = self.folder / "outputs"
        host.EXPORT_DIR.mkdir()
        host.SAMPLE = host.ORIGINAL_DIR / "not-used.xlsx"
        host.DESKTOP_MODE = False
        for name in ("WORKBOOKS", "HISTORY_STORES", "EXPORT_SERVICES", "MIGRATION_SERVICES",
                     "EXPORT_PREVIEWS", "DATASET_SERVICES", "OPERATIONS_STORES", "WRITEBACK_SERVICES"):
            getattr(host, name).clear()
        self.server = host.ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def call(self, path, body=None, origin=None):
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if origin:
            headers["Origin"] = origin
        request = Request(self.url + path, data=json.dumps(body, ensure_ascii=False, allow_nan=False).encode() if body is not None else None,
                          headers=headers)
        try:
            with urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def success(self, path, body=None):
        status, payload = self.call(path, body)
        self.assertEqual(status, 200, payload)
        return payload

    def refused(self, path, body, message=None):
        status, payload = self.call(path, body)
        self.assertEqual(status, 400, payload)
        if message:
            self.assertIn(message, payload["error"])
        return payload

    def workbook(self, filename="合成经营.xlsx", rows=None):
        path = host.ORIGINAL_DIR / filename
        workbook = openpyxl.Workbook()
        workbook.active.title = "业务"
        for row in rows or [["商品ID", "规格", "月计划", "仓库ID", "日期"],
                            ["000001", "合成规格", 25, "W1", "2026-11-01"]]:
            workbook.active.append(row)
        workbook.save(path)
        workbook.close()
        return path, self.success("/api/open", {"path": str(path)})

    def shipment(self, **changes):
        return {"businessId": "S1", "product": "RAW_SHIP", "sourceSystem": "synthetic",
                "warehouse": "合成来源仓", "date": "2026-10-30", "originalDate": "2026-10-30",
                "quantity": 10, "unit": "件", "baseUnit": "件", "status": "已发货", **changes}

    def aftersale(self, **changes):
        return {"businessId": "A1", "shipmentId": "S1", "product": "RAW_RETURN", "sourceSystem": "synthetic",
                "warehouse": "合成来源仓", "date": "2026-11-02", "originalDate": "2026-10-30",
                "quantity": 2, "unit": "件", "baseUnit": "件", "status": "已完成", "type": "return", "groupId": "GROUP1", **changes}

    def preview(self, kind, records, coverage=False):
        body = {"kind": kind, "records": records, "previewOnly": True}
        if coverage:
            body["coverage"] = {"start": "2026-10-01", "end": "2026-11-30", "complete": True,
                                "evidence": "合成来源完整导出与核对范围", "zeroRecordsConfirmed": not records}
        return self.success("/api/operations/import", body)

    def commit(self, preview):
        return self.success("/api/operations/import", {"token": preview["token"], "previewHash": preview["previewHash"],
                                                       "onConflict": "replace", "previewOnly": False, "confirmed": True})

    def catalog(self):
        for source in ("RAW_SHIP", "RAW_RETURN"):
            self.success("/api/daily/catalog", {"record": {"sourceProduct": source, "sourceSystem": "synthetic",
                "sourceShop": "合成来源仓", "sourceSpecification": "", "product": "STD_SKU_0001", "name": "合成标准商品",
                "specification": "统一规格", "shop": "合成标准仓", "warehouseId": "W1"}})

    def template(self, inspection):
        return self.success("/api/writeback/template", {"workbookId": inspection["workbookId"], "sheet": "业务",
             "keyColumns": {"product": "A", "specification": "B", "warehouseId": "D", "date": "E"}, "fields": {"plan": "C"}})

    def write_preview(self, inspection, template, value=30):
        return self.success("/api/writeback/preview", {"workbookId": inspection["workbookId"], "templateId": template["template"]["id"],
             "changes": [{"keys": template["rows"][0]["keys"], "fields": {"plan": value}}]})

    def enable_writeback(self):
        return self.success("/api/writeback/settings", {"enabled": True, "confirmed": True})

    def test_operations_confirmation_idempotence_revision_and_stale_token(self):
        preview = self.preview("shipments", [self.shipment()])
        self.assertEqual(self.success("/api/operations")["total"], 0)
        for confirmed in (False, None, "true"):
            self.refused("/api/operations/import", {"token": preview["token"], "previewHash": preview["previewHash"],
                                                   "previewOnly": False, "confirmed": confirmed}, "确认")
        self.assertEqual(self.commit(preview)["saved"], 1)
        repeated = self.preview("shipments", [self.shipment()])
        self.assertEqual(repeated["reviewCounts"]["unchanged"], 1)
        self.assertEqual(self.commit(repeated)["saved"], 0)
        stale = self.preview("shipments", [self.shipment(quantity=15)])
        changed = self.preview("shipments", [self.shipment(quantity=12)])
        self.commit(changed)
        self.refused("/api/operations/import", {"token": stale["token"], "previewHash": stale["previewHash"],
                                               "previewOnly": False, "confirmed": True}, "重新预览")
        records = self.success("/api/operations?kind=shipments")["items"]
        self.assertEqual((len(records), records[0]["quantity"], records[0]["revision"]), (1, 12, 2))
        revisions = self.success("/api/operations/revisions?id=" + records[0]["id"])
        self.assertEqual(revisions["items"][0]["record"]["quantity"], 10)

    def test_source_change_after_operations_preview_cannot_save_facts(self):
        path, inspection = self.workbook("合成发货.xlsx", [["发货明细ID", "商品ID", "发货日期", "数量", "单位", "统一单位", "状态"],
                                                             ["S1", "000001", "2026-11-01", 4, "件", "件", "已发货"]])
        preview = self.success("/api/operations/import", {"workbookId": inspection["workbookId"], "kind": "shipments", "previewOnly": True})
        workbook = openpyxl.load_workbook(path)
        workbook["业务"]["D2"] = 99
        workbook.save(path)
        workbook.close()
        self.refused("/api/operations/import", {"token": preview["token"], "previewHash": preview["previewHash"],
                                               "previewOnly": False, "confirmed": True}, "原表已变化")
        self.assertEqual(self.success("/api/operations")["total"], 0)

    def test_dual_period_reconciliation_and_excel_export_use_shared_catalog(self):
        self.catalog()
        self.commit(self.preview("shipments", [self.shipment()], coverage=True))
        self.commit(self.preview("aftersales", [self.aftersale(), self.aftersale(businessId="A2", type="refund")], coverage=True))
        scope = {"start": "2026-10-01", "end": "2026-10-31", "asOf": "2026-11-30"}
        report = self.success("/api/operations/reconcile", scope)
        self.assertTrue(report["complete"], report)
        self.assertEqual(len(report["items"]), 1)
        row = report["items"][0]
        self.assertEqual((row["product"], row["warehouseId"], row["originalNetQuantity"], row["occurrenceNetQuantity"]),
                         ("STD_SKU_0001", "W1", 8, 10))
        november = self.success("/api/operations/reconcile", {"start": "2026-11-01", "end": "2026-11-30"})
        self.assertEqual(november["items"][0]["occurrenceNetQuantity"], -2)
        exported = self.success("/api/operations/export", {**scope, "items": [{"netQuantity": 999999}]})
        workbook = openpyxl.load_workbook(io.BytesIO(base64.b64decode(exported["dataBase64"])), data_only=False)
        self.addCleanup(workbook.close)
        self.assertTrue({"来源汇总", "发货事实", "售后事实", "货损双轨", "净销量核对", "每日双视角", "待核对", "口径说明", "商品仓库关联"}.issubset(workbook.sheetnames))
        sheet = workbook["净销量核对"]
        headers = [c.value for c in sheet[1]]
        self.assertEqual(sheet.cell(2, headers.index("商品标识") + 1).value, "STD_SKU_0001")
        self.assertEqual(sheet.cell(2, headers.index("原销售期净销量") + 1).value, 8)
        self.assertEqual(sheet.cell(2, headers.index("售后发生期净销量") + 1).value, 10)

    def test_manual_loss_preview_save_and_shared_catalog_report(self):
        self.catalog()
        row = {"product": "RAW_SHIP", "sourceSystem": "synthetic", "warehouse": "合成来源仓", "date": "2026-11-01", "unit": "件",
               "registered": 2, "opening": 20, "inbound": 5, "outbound": 10, "physical": 13,
               "unitPrice": 7.7, "registeredAmount": 77}
        preview = self.preview("loss", [row])
        self.assertEqual(self.success("/api/operations/loss", {})["total"], 0)
        self.commit(preview)
        result = self.success("/api/operations/loss", {"start": "2026-11-01", "end": "2026-11-30"})["items"][0]
        self.assertEqual((result["product"], result["warehouseId"], result["rebuilt"], result["difference"], result["rebuiltAmount"], result["amountDifference"]),
                         ("STD_SKU_0001", "W1", 2, 0, 15.4, -61.6))
        self.assertEqual(result["status"], "awaiting_confirmation")

    def test_writeback_default_off_and_every_write_requires_explicit_confirmation(self):
        path, inspection = self.workbook()
        before = path.read_bytes()
        self.assertFalse(self.success("/api/writeback/settings")["enabled"])
        template = self.template(inspection)
        body = {"workbookId": inspection["workbookId"], "templateId": template["template"]["id"],
                "changes": [{"keys": template["rows"][0]["keys"], "fields": {"plan": 30}}]}
        self.refused("/api/writeback/preview", body, "开启")
        for confirmed in (False, None, "true"):
            self.refused("/api/writeback/settings", {"enabled": True, "confirmed": confirmed}, "确认")
        self.enable_writeback()
        preview = self.write_preview(inspection, template)
        for confirmed in (False, None, "true"):
            self.refused("/api/writeback/commit", {"token": preview["token"], "confirmed": confirmed}, "确认")
        self.assertEqual(path.read_bytes(), before)
        self.success("/api/writeback/settings", {"enabled": False})
        self.refused("/api/writeback/commit", {"token": preview["token"], "confirmed": True})
        self.assertEqual(path.read_bytes(), before)

    def test_writeback_source_external_change_after_preview_is_preserved(self):
        path, inspection = self.workbook()
        self.enable_writeback()
        preview = self.write_preview(inspection, self.template(inspection))
        workbook = openpyxl.load_workbook(path)
        workbook["业务"]["C2"] = 41
        workbook.save(path)
        workbook.close()
        changed = path.read_bytes()
        self.refused("/api/writeback/commit", {"token": preview["token"], "confirmed": True}, "变化")
        self.assertEqual(path.read_bytes(), changed)
        self.assertEqual(self.success("/api/writeback/history")["items"], [])

    def test_writeback_commit_backups_and_restore_return_exact_original_bytes(self):
        path, inspection = self.workbook()
        before = path.read_bytes()
        self.enable_writeback()
        template = self.template(inspection)
        preview = self.write_preview(inspection, template)
        self.assertEqual(preview["changes"][0]["cell"], "C2")
        committed = self.success("/api/writeback/commit", {"token": preview["token"], "confirmed": True})
        self.assertEqual(committed["status"], "success")
        self.assertEqual(Path(committed["backup"]).read_bytes(), before)
        book = openpyxl.load_workbook(path, data_only=False)
        self.assertEqual(book["业务"]["C2"].value, 30)
        self.assertEqual(book["业务"]["A2"].value, "000001")
        book.close()
        self.refused("/api/writeback/commit", {"token": preview["token"], "confirmed": True})
        self.refused("/api/writeback/restore", {"operationId": committed["id"], "confirmed": False})
        restored = self.success("/api/writeback/restore", {"operationId": committed["id"], "confirmed": True})
        self.assertEqual(restored["status"], "restored")
        self.assertEqual(path.read_bytes(), before)
        self.refused("/api/writeback/restore", {"operationId": committed["id"], "confirmed": True})
        self.assertEqual(self.success("/api/writeback/history")["items"][0]["status"], "restored")

    def test_restore_rejects_external_source_or_backup_changes(self):
        for change_backup in (False, True):
            with self.subTest(change_backup=change_backup):
                path, inspection = self.workbook("合成恢复-%s.xlsx" % change_backup)
                self.enable_writeback()
                preview = self.write_preview(inspection, self.template(inspection))
                committed = self.success("/api/writeback/commit", {"token": preview["token"], "confirmed": True})
                if change_backup:
                    Path(committed["backup"]).write_bytes(b"synthetic-corrupt-backup")
                else:
                    workbook = openpyxl.load_workbook(path)
                    workbook["业务"]["C2"] = 77
                    workbook.save(path)
                    workbook.close()
                current = path.read_bytes()
                self.refused("/api/writeback/restore", {"operationId": committed["id"], "confirmed": True}, "变化")
                self.assertEqual(path.read_bytes(), current)

    def test_cross_origin_business_write_is_rejected_before_persistence(self):
        status, payload = self.call("/api/operations/import", {"kind": "shipments", "records": [self.shipment()]}, origin="https://foreign.invalid")
        self.assertEqual(status, 403, payload)
        self.assertEqual(self.success("/api/operations")["total"], 0)

    def test_read_only_symlink_alias_cannot_be_promoted_to_source_writeback(self):
        path, _ = self.workbook()
        before = path.read_bytes()
        alias = host.ORIGINAL_DIR / "合成原件别名.xlsx"
        try:
            alias.symlink_to(path)
        except OSError as error:
            if getattr(error, "winerror", None) == 1314:
                self.skipTest("当前 Windows 账号未启用创建符号链接的权限。")
            raise
        inspection = self.success("/api/open", {"path": str(alias)})
        # Read-only intake remains available, but the write path must retain
        # evidence that the user selected a link rather than a plain original.
        self.enable_writeback()
        self.refused("/api/writeback/template", {"workbookId": inspection["workbookId"], "sheet": "业务",
            "keyColumns": {"product": "A"}, "fields": {"plan": "C"}}, "链接")
        self.assertEqual(path.read_bytes(), before)

    def test_read_only_alias_in_parent_directory_cannot_bypass_writeback_link_check(self):
        path, _ = self.workbook()
        before = path.read_bytes()
        alias_dir = self.folder / "来源目录别名"
        try:
            alias_dir.symlink_to(host.ORIGINAL_DIR, target_is_directory=True)
        except OSError as error:
            if getattr(error, "winerror", None) == 1314:
                self.skipTest("当前 Windows 账号未启用创建符号链接的权限。")
            raise
        inspection = self.success("/api/open", {"path": str(alias_dir / path.name)})
        self.enable_writeback()
        self.refused("/api/writeback/template", {"workbookId": inspection["workbookId"], "sheet": "业务",
            "keyColumns": {"product": "A"}, "fields": {"plan": "C"}}, "链接")
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
