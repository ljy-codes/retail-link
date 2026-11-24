"""v0.2 identity, warehouse and migration contracts on disposable data only."""
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import zipfile

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT
TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DEV))
from backend.analyzer import analyze_workbook
from backend.daily_import import save_catalog, list_catalog, import_catalog, apply_catalog
from backend.daily_records import DailyStore
from backend.dataset_service import DataSetService
from backend.migration_service import MigrationService, MigrationError
from backend.product_identity import resolve_identity


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="v02-identity-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = DailyStore(self.root / "data")

    def book(self, sheets, name="input.xlsx"):
        path = self.root / name
        book = openpyxl.Workbook()
        book.remove(book.active)
        for name, rows in sheets.items():
            sheet = book.create_sheet(name)
            for row in rows:
                sheet.append(row)
        book.save(path)
        book.close()
        return path

    def alias(self, **fields):
        return save_catalog(self.store, {"sourceProduct": "001", "sourceShop": "同名仓", "sourceSpecification": "1组", "specification": "1组", "product": "STD", **fields})["record"]

    def test_same_source_id_two_specs_import_as_two_real_products(self):
        path = self.book({"订货": [["商品ID", "商品名称", "商品规格", "仓库ID", "仓库名称"],
            ["001", "同名", "1组", "W1", "同名仓"], ["001", "同名", "1件", "W1", "同名仓"]]})
        result = import_catalog(self.store, {"path": path}, {"previewOnly": True})
        self.assertEqual((result["validCount"], result["invalidCount"]), (2, 0))
        self.assertEqual(len({r["product"] for r in result["records"]}), 2)
        import_catalog(self.store, {"path": path}, {"previewOnly": False, "previewFingerprint": result["previewFingerprint"]})
        self.assertEqual(list_catalog(self.store)["total"], 2)
        matched = resolve_identity(list_catalog(self.store)["items"], "001", "同名仓", "1件", warehouse_id="W1")
        self.assertEqual(matched["identityStatus"], "mapped")
        self.assertIn("1件", matched["product"])

    def test_missing_spec_does_not_choose_one_of_two_aliases(self):
        self.alias(sourceSpecification="1件", specification="1件", product="EACH")
        self.alias(product="GROUP")
        result = apply_catalog(self.store, {"records": [{"date": "2025-11-01", "product": "001", "shop": "同名仓", "price": 3, "salesQuantity": 1, "source": {}}], "issues": [], "duplicateCount": 0})
        self.assertEqual(result["validCount"], 0)
        self.assertIn("多个规格", result["issues"][0]["message"])

    def test_same_name_warehouse_ids_are_distinct_and_rename_keeps_alias(self):
        first = self.alias(sourceWarehouseId="W1", warehouseId="W1")
        self.alias(sourceWarehouseId="W2", warehouseId="W2")
        catalog = list_catalog(self.store)["items"]
        self.assertEqual(resolve_identity(catalog, "001", "同名仓", "1组")["identityStatus"], "ambiguous")
        changed = save_catalog(self.store, first | {"sourceShop": "已改名", "shop": "标准新名"}, expected_revision=first["revision"])["record"]
        self.assertEqual(changed["catalogId"], first["catalogId"])
        self.assertEqual(list_catalog(self.store)["total"], 2)
        self.assertEqual(resolve_identity(list_catalog(self.store)["items"], "001", "任意旧名", "1组", warehouse_id="W1")["warehouse"], "标准新名")

    def test_stable_source_sku_survives_source_spec_label_change(self):
        first = self.alias(sourceSku="SKU-01", sourceWarehouseId="W1", warehouseId="W1")
        changed = save_catalog(self.store, first | {"sourceSpecification": "新标签"}, expected_revision=1)["record"]
        self.assertEqual(first["catalogId"], changed["catalogId"])
        match = resolve_identity(list_catalog(self.store)["items"], "001", "同名仓", "旧标签", "SKU-01", "W1")
        self.assertEqual(match["product"], "STD")
        self.assertEqual(match["identityStatus"], "mapped")

    def test_different_standard_specs_cannot_share_one_standard_sku(self):
        self.alias()
        with self.assertRaisesRegex(ValueError, "不同规格"):
            self.alias(sourceProduct="002", specification="1件")

    def test_migration_rejects_a_forged_daily_identity_key(self):
        self.store.save({"date": "2025-11-01", "product": "001", "price": 1, "warehouseId": "W1"})
        with self.store._connect() as db:
            db.execute("UPDATE daily_records SET identityKey=?", ["0" * 64])
        with self.assertRaisesRegex(MigrationError, "去重键"):
            MigrationService._validate_database(self.store.path)

    def test_daily_two_ids_and_two_specs_do_not_overwrite_each_other(self):
        base = {"date": "2025-11-01", "product": "001", "shop": "同名仓", "price": 3}
        for warehouse, spec in (("W1", "1件"), ("W2", "1件"), ("W1", "1组")):
            self.store.save(base | {"warehouseId": warehouse, "specification": spec})
        self.assertEqual(self.store.list()["total"], 3)
        result = self.store.save(base | {"warehouseId": "W1", "specification": "1件", "shop": "改名仓", "price": 4}, "replace")
        self.assertEqual(result["status"], "updated")
        self.assertEqual(self.store.list()["total"], 3)
        self.assertEqual(self.store.revisions(result["record"]["id"])["items"][0]["record"]["price"], 3)

    def test_legacy_database_upgrade_keeps_ids_values_and_revisions(self):
        path = self.root / "legacy"
        path.mkdir()
        ident = "a" * 32
        with sqlite3.connect(path / "daily.sqlite3") as db:
            db.executescript('''CREATE TABLE daily_records(id TEXT PRIMARY KEY,date TEXT,product TEXT,name TEXT,shop TEXT,price REAL,salesQuantity REAL,source TEXT,createdAt TEXT,updatedAt TEXT,deleted INTEGER,revision INTEGER,UNIQUE(date,product,shop));
                CREATE TABLE daily_revisions(seq INTEGER PRIMARY KEY AUTOINCREMENT,recordId TEXT,savedAt TEXT,action TEXT,snapshot TEXT);
                CREATE TABLE product_catalog(sourceProduct TEXT,sourceShop TEXT,product TEXT,name TEXT,specification TEXT,shop TEXT,source TEXT,revision INTEGER,updatedAt TEXT,PRIMARY KEY(sourceProduct,sourceShop));''')
            db.execute("INSERT INTO daily_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", [ident, "2025-11-01", "OLD", "旧名", "旧仓", 0, None, '"手工"', "2025-11-01", "2025-11-01", 0, 3])
            db.execute("INSERT INTO product_catalog VALUES(?,?,?,?,?,?,?,?,?)", ["OLD", "旧仓", "STANDARD", "旧名", "1件", "标准仓", '"人工"', 2, "2025-11-01T00:00:00+08:00"])
        self.assertEqual(MigrationService._validate_database(path / "daily.sqlite3")["dailyRecords"], 1)
        upgraded = DailyStore(path)
        old = upgraded.get(ident)
        self.assertEqual((old["price"], old["salesQuantity"], old["revision"], old["warehouseId"]), (0, None, 3, ""))
        self.assertEqual(list_catalog(upgraded)["items"][0]["revision"], 2)
        self.assertEqual(MigrationService._validate_database(upgraded.path)["productCatalog"], 1)
        self.assertEqual(DailyStore(path).list()["total"], 1)

    def analyze(self, warehouse_sales=True):
        catalog = [self.alias(sourceWarehouseId=w, warehouseId=w) for w in ("N", "W")]
        sales = [["商品", "规格", "仓库ID", "仓库", "日期", "数量"], ["001", "1组", "N" if warehouse_sales else "", "同名仓" if warehouse_sales else "平台", "2026-11-19", 20]]
        path = self.book({"计划": [["商品", "月计划"], ["STD", 1000]],
            "库存": [["商品", "规格", "仓库ID", "仓库", "库存", "单价"], ["001", "1组", "N", "同名仓", 100, 10], ["001", "1组", "W", "同名仓", 200, 10]], "销售": sales})
        config = {"asOf": "2026-11-20", "period": "2026-11", "averageDays": 1, "inventoryComparable": True, "planBasis": "sales_quantity", "productCatalog": catalog,
            "mappings": {"summary": {"sheet": "计划", "columns": {"product": "A", "plan": "B"}},
                         "inventory": {"sheet": "库存", "columns": {"product": "A", "specification": "B", "warehouseId": "C", "warehouse": "D", "stockSmall": "E", "basePrice": "F"}},
                         "sales": {"sheet": "销售", "columns": {"product": "A", "specification": "B", "warehouseId": "C", "warehouse": "D", "date": "E", "quantity": "F"}}}}
        return analyze_workbook(path, config)

    def test_main_analysis_and_daily_share_catalog_and_keep_warehouse_stock(self):
        result = self.analyze()
        self.assertEqual(len(result["products"]), 1)
        product = result["products"][0]
        self.assertEqual((product["id"], product["plan"], product["stockSmall"]), ("STD", 1000, 300))
        stocks = {item["warehouseId"]: item for item in product["warehouseStocks"]}
        self.assertEqual((stocks["N"]["stockSmall"], stocks["W"]["stockSmall"]), (100, 200))
        self.assertEqual(stocks["N"]["coverageDays"], 5)
        self.assertIsNone(stocks["W"]["coverageDays"])
        self.assertIsNone(product["coverageDays"])
        self.assertTrue(product["identityLinks"])

    def test_platform_sales_are_not_allocated_to_each_warehouse(self):
        result = self.analyze(warehouse_sales=False)
        for product in result["products"]:
            for stock in product["warehouseStocks"]:
                self.assertIsNone(stock["coverageDays"])

    def test_nonpositive_plan_invalidates_total_achievement(self):
        path = self.book({"计划": [["商品", "月计划"], ["A", 0], ["B", 100]], "销售": [["商品", "日期", "数量"], ["A", "2026-11-19", 200], ["B", "2026-11-19", 100]]})
        result = analyze_workbook(path, {"period": "2026-11", "asOf": "2026-11-20", "planBasis": "sales_quantity"})
        self.assertEqual(result["summary"]["unknownTargetCount"], 1)
        self.assertIsNone(result["summary"]["achievement"])

    def test_aliases_do_not_duplicate_standard_plan_or_inventory(self):
        catalog = [self.alias(sourceProduct=p) for p in ("001", "002")]
        path = self.book({"计划": [["商品", "月计划"], ["001", 100], ["002", 100]]})
        result = analyze_workbook(path, {"period": "2026-11", "asOf": "2026-11-20", "productCatalog": catalog})
        self.assertEqual(len(result["products"]), 1)
        self.assertEqual(result["products"][0]["plan"], 100)
        self.assertTrue(any(i["code"] == "DUPLICATE_PRODUCT" for i in result["issues"]))
        path = self.book({"计划": [["商品", "月计划"], ["001", 100], ["002", 200]]})
        conflicting = analyze_workbook(path, {"period": "2026-11", "asOf": "2026-11-20", "productCatalog": catalog})
        self.assertIsNone(conflicting["products"][0]["plan"])
        self.assertIsNone(conflicting["summary"]["achievement"])

    def test_dataset_preserves_same_name_distinct_warehouse_ids(self):
        path = self.book({"库存": [["商品", "仓库", "仓库ID", "库存", "单价"], ["A", "同名仓", "N", 100, 10], ["A", "同名仓", "W", 200, 10]]})
        service = DataSetService(self.root / "dataset", lambda key: {"path": path, "filename": path.name, "sha256": service._sha(path), "size": path.stat().st_size})
        staged = service.stage(["book"])
        selection = [{"sourceId": staged["sheets"][0]["sourceId"], "role": "inventory", "headerRow": 1, "snapshotDate": "2026-11-20", "columns": {"product": "A", "warehouse": "B", "warehouseId": "C", "stockSmall": "D", "basePrice": "E"}}]
        preview = service.preview(staged["sessionId"], selection, {"period": "2026-11", "asOf": "2026-11-20"})
        self.assertTrue(preview["canContinue"], preview["issues"])
        materialized = service.materialize(preview["token"])
        result = analyze_workbook(materialized["path"], materialized["config"])
        self.assertEqual({s["warehouseId"] for s in result["products"][0]["warehouseStocks"]}, {"N", "W"})

    def test_operations_migration_is_validated_and_writeback_excluded(self):
        source = self.root / "data"
        (source / "operations").mkdir()
        state = {"version": 1, "generation": 0, "records": [], "revisions": [], "imports": []}
        (source / "operations/state.json").write_text(json.dumps(state))
        (source / "writeback").mkdir()
        (source / "writeback/settings.json").write_text('{"enabled":true}')
        (source / "writeback/original.xlsx").write_bytes(b"not-portable")
        service = MigrationService(source)
        bundle = service.export_bundle(self.root / "portable.zip")
        with zipfile.ZipFile(bundle["path"]) as archive:
            self.assertIn("operations/state.json", archive.namelist())
            self.assertFalse(any(name.startswith("writeback/") for name in archive.namelist()))
        target = MigrationService(self.root / "target")
        preview = target.preview_import(bundle["path"])
        target.import_bundle(bundle["path"], confirmation_token=preview["token"])
        self.assertEqual(json.loads((self.root / "target/operations/state.json").read_text()), state)
        (source / "operations/state.json").write_text(json.dumps(state | {"version": 99}))
        with self.assertRaisesRegex(MigrationError, "经营记录"):
            service.export_bundle(self.root / "invalid.zip")


if __name__ == "__main__":
    unittest.main(verbosity=2)
