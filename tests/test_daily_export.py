"""Daily ledger exports and catalog migration, using fictional isolated data."""
import base64
from contextlib import closing
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import sys
import unittest

import openpyxl
from tests.temp_support import temporary_directory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.daily_records import DailyStore
from backend.daily_export import export_daily
from backend.migration_service import MigrationService


class DailyExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temporary_directory(prefix="daily-export-", directory=Path(__file__).resolve().parent)
        self.root = Path(self.tmp.name)
        self.store = DailyStore(self.root / "data")
        self.store.save({"date":"2025-11-01", "product":"000001", "name":"虚构A", "shop":"测试仓", "price":0, "salesQuantity":None})
        self.store.save({"date":"2025-11-02", "product":"=1+2", "name":"=HYPERLINK(1)", "shop":"测试仓", "price":3.6, "salesQuantity":0})
        self.store.save({"date":"2025-11-03", "product":"OTHER", "shop":"另一仓", "price":1/3, "salesQuantity":2})

    def tearDown(self):
        self.tmp.cleanup()

    def book(self, query=None):
        result = export_daily(self.store, query or {}, "xlsx")
        book = openpyxl.load_workbook(BytesIO(base64.b64decode(result["dataBase64"])))
        self.addCleanup(book.close)
        return result, book

    def catalog(self):
        with self.store._connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS product_catalog (sourceProduct TEXT NOT NULL,sourceShop TEXT NOT NULL,product TEXT NOT NULL,name TEXT NOT NULL,specification TEXT NOT NULL,shop TEXT NOT NULL,source TEXT NOT NULL,revision INTEGER NOT NULL,updatedAt TEXT NOT NULL,PRIMARY KEY(sourceProduct,sourceShop))")
            connection.execute("INSERT INTO product_catalog VALUES (?,?,?,?,?,?,?,?,?)", ["00001","测试仓","STANDARD-1","虚构关联","1x12","标准仓",json.dumps({"type":"test"}),1,"2026-11-23T12:00:00+08:00"])

    def test_dates_ids_formula_like_text_zero_and_blank_survive_xlsx(self):
        result, book = self.book()
        self.assertEqual(result["count"], 3)
        self.assertEqual(book.sheetnames, ["每日记录","导出范围","商品关联"])
        sheet = book["每日记录"]
        self.assertEqual(sheet["A4"].value.year, 2025)
        self.assertEqual(sheet["B4"].value, "000001")
        self.assertEqual(sheet["B3"].value, "=1+2")
        self.assertEqual(sheet["B3"].data_type, "s")
        self.assertEqual(sheet["C3"].data_type, "s")
        self.assertIsNone(sheet["F4"].value)
        self.assertEqual(sheet["E4"].value, 0)
        self.assertEqual(sheet["F3"].value, 0)

    def test_filter_is_applied_and_scope_is_exported(self):
        result, book = self.book({"start":["2025-11-01"], "end":["2025-11-01"], "shop":["测试仓"]})
        self.assertEqual(result["count"], 1)
        self.assertEqual(book["每日记录"]["B2"].value, "000001")
        self.assertEqual(result["scope"]["开始日期"], "2025-11-01")
        self.assertIn("未自动扣除售后", result["scope"]["销量口径"])

    def test_csv_has_scope_and_does_not_evaluate_text(self):
        result = export_daily(self.store, {}, "csv")
        content = base64.b64decode(result["dataBase64"]).decode("utf-8-sig")
        self.assertIn("记录数量,3", content)
        self.assertIn("'=1+2", content)
        self.assertIn("000001", content)

    def test_complete_filter_exports_more_than_one_page(self):
        with self.store._connect() as connection:
            for i in range(1002):
                from backend.daily_records import _normalized
                self.store._save(connection, _normalized({"date":"2025-11-01","product":f"PAGE-{i:04d}","price":1}))
        result, book = self.book({"product":["PAGE-"]})
        self.assertEqual(result["count"], 1002)
        self.assertEqual(book["每日记录"].max_row, 1003)

    def test_empty_export_is_valid_and_deleted_records_are_explicit(self):
        result, book = self.book({"product":["absent"]})
        self.assertEqual(result["count"], 0)
        self.assertEqual(book["每日记录"].max_row, 1)
        record = self.store.list(product="000001")["items"][0]
        self.store.set_deleted(record["id"], True)
        result, book = self.book({"deleted":["1"]})
        self.assertEqual(result["count"], 1)
        self.assertEqual(book["每日记录"]["G2"].value, "已删除")

    def test_catalog_is_exported_as_text_and_migrates(self):
        self.catalog()
        _, book = self.book()
        self.assertEqual(book["商品关联"]["A2"].value, "00001")
        self.assertEqual(book["商品关联"]["A2"].data_type, "s")
        service = MigrationService(self.root / "data")
        bundle = service.export_bundle(self.root / "migration.zip")
        self.assertEqual(bundle["counts"]["productCatalog"], 1)
        target = MigrationService(self.root / "target")
        preview = target.preview_import(bundle["path"])
        self.assertEqual(preview["counts"]["productCatalog"], 1)
        target.import_bundle(bundle["path"], confirmation_token=preview["token"])
        with closing(sqlite3.connect(self.root / "target/daily.sqlite3")) as connection:
            self.assertEqual(connection.execute("SELECT product FROM product_catalog").fetchone()[0], "STANDARD-1")

    def test_old_database_without_catalog_is_still_migratable(self):
        with self.store._connect() as connection:
            connection.execute("DROP TABLE IF EXISTS product_catalog")
        self.assertEqual(MigrationService._validate_database(self.store.path)["productCatalog"], 0)

    def test_invalid_catalog_is_rejected(self):
        self.catalog()
        with self.store._connect() as connection:
            connection.execute("UPDATE product_catalog SET product='' ")
        with self.assertRaisesRegex(ValueError,"商品关联"):
            MigrationService._validate_database(self.store.path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
