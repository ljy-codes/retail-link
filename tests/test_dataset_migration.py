"""Real import-template migration regression using disposable fictional workbooks."""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
from pathlib import Path
import sys
import unittest
import zipfile

import openpyxl
from tests.temp_support import temporary_directory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class DatasetMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory(prefix="dataset-migration-", directory=Path(__file__).parent)
        self.root = Path(self.temp.name)
        self.dataset_module = importlib.import_module("backend.dataset_service")
        self.migration_module = importlib.import_module("backend.migration_service")
        self.analyzer = importlib.import_module("backend.analyzer")
        self.origin = self.root / "origin"
        self.origin.mkdir()
        self.source = self.make_template(self.origin, "待迁移格式", "SKU-001")
        self.migration = self.migration_module.MigrationService(self.origin)

    def tearDown(self):
        self.temp.cleanup()

    def make_template(self, data_dir, name, product):
        path = self.root / (name + ".xlsx")
        workbook = openpyxl.Workbook()
        workbook.active.title = "月计划"
        workbook.active.append(["商品标识", "计划金额"])
        workbook.active.append([product, 1000])
        workbook.save(path)
        workbook.close()
        entry = {"path": path, "filename": path.name,
                 "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size": path.stat().st_size}
        service = self.dataset_module.DataSetService(data_dir, lambda _: entry)
        staged = service.stage(["fixture-workbook"])
        choice = {"sourceId": staged["sheets"][0]["sourceId"], "role": "summary", "headerRow": 1,
                  "columns": {"product": "A", "plan": "B"}, "unit": "small"}
        config = dict(staged["config"], asOf="2026-11-20", period="2026-11")
        preview = service.preview(staged["sessionId"], [choice], config)
        self.assertTrue(preview["canContinue"], preview)
        generated = service.materialize(preview["token"])
        self.assertEqual(self.analyzer.analyze_workbook(generated["path"], generated["config"])["products"][0]["id"], product)
        service.accept(preview["token"], name)
        return service

    def bundle(self):
        return self.migration.export_bundle(self.root / "迁移包.zip")

    def rewrite_template(self, bundle, filename, mutate):
        """Keep ZIP integrity valid so rejection exercises template schema validation."""
        with zipfile.ZipFile(bundle) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        templates = json.loads(entries["import_templates.json"])
        mutate(templates)
        content = json.dumps(templates, ensure_ascii=False).encode("utf-8")
        entries["import_templates.json"] = content
        manifest = json.loads(entries["manifest.json"])
        entry = next(item for item in manifest["files"] if item["path"] == "import_templates.json")
        entry["bytes"] = len(content)
        entry["sha256"] = hashlib.sha256(content).hexdigest()
        entries["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        modified = self.root / filename
        with zipfile.ZipFile(modified, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, payload in entries.items():
                archive.writestr(name, payload)
        return modified

    def test_export_and_empty_target_import_preserve_real_dataset_templates(self):
        exported = self.bundle()
        self.assertEqual(exported["counts"]["importTemplates"], 1)
        with zipfile.ZipFile(exported["path"]) as archive:
            self.assertIn("import_templates.json", archive.namelist())
            self.assertFalse(any(name.endswith(".xlsx") for name in archive.namelist()))
            self.assertEqual(archive.read("import_templates.json"), (self.origin / "import_templates.json").read_bytes())
        target = self.migration_module.MigrationService(self.root / "empty-target")
        preview = target.preview_import(exported["path"])
        self.assertTrue(preview["canImport"])
        self.assertEqual(preview["counts"]["importTemplates"], 1)
        self.assertFalse((target.data_dir / "import_templates.json").exists())
        imported = target.import_bundle(exported["path"], confirmation_token=preview["token"])
        self.assertEqual(imported["currentCounts"]["importTemplates"], 1)
        target_dataset = self.dataset_module.DataSetService(target.data_dir, lambda _: None)
        self.assertEqual(target_dataset.templates(), self.source.templates())
        self.assertEqual((target.data_dir / "import_templates.json").read_bytes(), (self.origin / "import_templates.json").read_bytes())

    def test_malformed_template_structures_are_rejected_without_changing_existing_data(self):
        exported = self.bundle()
        target_dir = self.root / "existing-target"
        local_dataset = self.make_template(target_dir, "本地保留格式", "LOCAL-001")
        target = self.migration_module.MigrationService(target_dir)
        before = (target_dir / "import_templates.json").read_bytes()
        before_templates = local_dataset.templates()
        mutations = [lambda value: value.update(version=99),
                     lambda value: value.update(templates={}),
                     lambda value: value["templates"][0].update(structures=[]),
                     lambda value: value["templates"][0].update(selections=[])]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                corrupt = self.rewrite_template(exported["path"], f"损坏结构-{index}.zip", mutate)
                with self.assertRaises(ValueError):
                    target.preview_import(corrupt)
                self.assertEqual((target_dir / "import_templates.json").read_bytes(), before)
                self.assertEqual(local_dataset.templates(), before_templates)

    def test_duplicate_template_ids_are_rejected_without_changing_existing_data(self):
        exported = self.bundle()
        target_dir = self.root / "duplicate-target"
        local_dataset = self.make_template(target_dir, "重复测试本地格式", "LOCAL-002")
        target = self.migration_module.MigrationService(target_dir)
        before = (target_dir / "import_templates.json").read_bytes()
        def duplicate(value):
            second = copy.deepcopy(value["templates"][0])
            second["name"] = "同 ID 的另一名称"
            value["templates"].append(second)
        corrupt = self.rewrite_template(exported["path"], "重复模板ID.zip", duplicate)
        with self.assertRaises(ValueError):
            target.preview_import(corrupt)
        self.assertEqual((target_dir / "import_templates.json").read_bytes(), before)
        self.assertEqual(local_dataset.templates()[0]["name"], "重复测试本地格式")

    def test_local_template_change_after_preview_invalidates_import_token(self):
        exported = self.bundle()
        target_dir = self.root / "changed-target"
        target = self.migration_module.MigrationService(target_dir)
        preview = target.preview_import(exported["path"])
        local_dataset = self.make_template(target_dir, "预览后新增格式", "LOCAL-003")
        before = (target_dir / "import_templates.json").read_bytes()
        with self.assertRaises(ValueError):
            target.import_bundle(exported["path"], replace=True, confirmation_token=preview["token"])
        self.assertEqual((target_dir / "import_templates.json").read_bytes(), before)
        self.assertEqual([template["name"] for template in local_dataset.templates()], ["预览后新增格式"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
