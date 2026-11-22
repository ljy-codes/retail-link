"""Real-file migration behavior tests, using only disposable fictional records."""
from __future__ import annotations

from contextlib import closing
import hashlib
import importlib
import json
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="migration-", dir=Path(__file__).parent)
        self.root = Path(self.temp.name)
        self.origin = self.root / "origin"
        self.output = self.root / "output"
        self.output.mkdir()
        self.module = importlib.import_module("backend.migration_service")
        self.daily_module = importlib.import_module("backend.daily_records")
        self.origin.mkdir()
        self._seed(self.origin, "111", price=12.5)
        self.service = self.module.MigrationService(self.origin)

    def tearDown(self):
        self.temp.cleanup()

    def _seed(self, directory, identifier, price):
        directory.mkdir(parents=True, exist_ok=True)
        history = directory / "history"
        deleted = directory / "history_deleted"
        history.mkdir(exist_ok=True)
        deleted.mkdir(exist_ok=True)
        (history / (identifier + ".json")).write_text(json.dumps({
            "id": identifier, "filename": "业务数据.xlsx", "summary": {"plan": 1000},
            "products": [{"id": "001", "name": "虚构商品"}],
            "sourceInfo": {"path": str(self.root / "outside-source.xlsx"), "sha256": "e" * 64},
        }, ensure_ascii=False))
        (deleted / (identifier + "9.json")).write_text(json.dumps({"id": identifier + "9", "summary": {}, "products": []}))
        store = self.daily_module.DailyStore(directory)
        store.save({"date": "2026-11-20", "product": "001", "name": "虚构商品", "shop": "测试店", "price": price, "salesQuantity": 3})
        prefs = {"version": 1, "profiles": [{"name": "分析口径", "config": {"targetRate": .85}}],
                 "appearance": {"mode": "dark", "palette": "focus", "glass": False},
                 "dailyMapping": {"sheet": "表一", "columns": {"product": "A", "price": "B"}}}
        (directory / "preferences.json").write_text(json.dumps(prefs, ensure_ascii=False))
        logs = directory / "logs"
        logs.mkdir(exist_ok=True)
        (logs / "operations.jsonl").write_text(json.dumps({"operation": "export", "operationId": "untrusted-rollback",
            "target": str(self.root / "do-not-change.xlsx"), "result": "success", "time": "2026-11-20T12:00:00+08:00"}) + "\n")
        return prefs

    def _bundle(self, name="迁移.zip"):
        return Path(self.service.export_bundle(self.output / name)["path"])

    def _target(self, name="target"):
        return self.module.MigrationService(self.root / name)

    def _rewrite_zip(self, source, name, mutate):
        with zipfile.ZipFile(source) as archive:
            entries = [(info.filename, archive.read(info.filename), info) for info in archive.infolist()]
        entries = mutate(entries)
        destination = self.output / name
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
                for filename, content, info in entries:
                    if info is None:
                        archive.writestr(filename, content)
                    else:
                        info.filename = filename
                        archive.writestr(info, content)
        return destination

    def _manifest_change(self, source, name, mutate):
        def change(entries):
            result = []
            for filename, data, info in entries:
                if filename == "manifest.json":
                    manifest = json.loads(data)
                    mutate(manifest)
                    data = json.dumps(manifest).encode()
                result.append((filename, data, info))
            return result
        return self._rewrite_zip(source, name, change)

    def test_export_contains_managed_data_and_preferences_but_not_source_or_generated_files(self):
        (self.origin / "source.xlsx").write_bytes(b"source-not-managed")
        (self.origin / "exports").mkdir()
        (self.origin / "exports" / "generated.xlsx").write_bytes(b"generated-not-managed")
        source = self.root / "outside-source.xlsx"
        source.write_bytes(b"source-outside")
        digest = sha(source)
        path = self._bundle()
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            for expected in ("history/111.json", "history_deleted/1119.json", "daily.sqlite3", "preferences.json", "manifest.json"):
                self.assertIn(expected, names)
            self.assertFalse(any(name.endswith(".xlsx") for name in names))
            manifest = json.loads(archive.read("manifest.json"))
            for entry in manifest["files"]:
                self.assertEqual(entry["sha256"], hashlib.sha256(archive.read(entry["path"])).hexdigest())
        self.assertEqual(sha(source), digest)

    def test_export_uses_sqlite_backup_and_keeps_committed_wal_rows(self):
        conn = sqlite3.connect(self.origin / "daily.sqlite3")
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            conn.execute("UPDATE daily_records SET price=18.75")
            conn.commit()
            self.assertTrue((self.origin / "daily.sqlite3-wal").exists())
            path = self._bundle()
            target = self._target()
            preview = target.preview_import(path)
            target.import_bundle(path, confirmation_token=preview["token"])
            with closing(sqlite3.connect(target.data_dir / "daily.sqlite3")) as imported:
                self.assertEqual(imported.execute("SELECT price FROM daily_records").fetchone()[0], 18.75)
        finally:
            conn.close()

    def test_preview_is_read_only_and_import_requires_its_confirmation(self):
        path = self._bundle()
        target = self._target()
        with self.assertRaises(ValueError):
            target.import_bundle(path)
        preview = target.preview_import(path)
        self.assertTrue(preview["canImport"])
        self.assertFalse((target.data_dir / "daily.sqlite3").exists())
        self.assertFalse((target.data_dir / "history" / "111.json").exists())
        result = target.import_bundle(path, confirmation_token=preview["token"])
        self.assertEqual(result["result"], "success")
        self.assertEqual(result["preferences"]["profiles"][0]["name"], "分析口径")
        self.assertEqual(result["preferences"]["appearance"]["mode"], "dark")
        with self.assertRaises(ValueError):
            target.import_bundle(path, confirmation_token=preview["token"])

    def test_imported_logs_are_archived_not_executable_rollback_history(self):
        path = self._bundle()
        target = self._target()
        preview = target.preview_import(path)
        target.import_bundle(path, confirmation_token=preview["token"])
        archived = list((target.data_dir / "logs" / "imported").glob("*.jsonl"))
        self.assertEqual(len(archived), 1)
        self.assertIn("untrusted-rollback", archived[0].read_text())
        self.assertNotIn("untrusted-rollback", (target.data_dir / "logs" / "operations.jsonl").read_text())

    def test_export_does_not_clobber_existing_bundle(self):
        path = self._bundle()
        before = sha(path)
        with self.assertRaises(FileExistsError):
            self.service.export_bundle(path)
        self.assertEqual(sha(path), before)
        self.assertEqual(list(self.output.glob("*.tmp")), [])

    def test_conflicting_daily_database_requires_explicit_replacement_and_preserves_backup(self):
        path = self._bundle()
        target_dir = self.root / "existing"
        self._seed(target_dir, "222", price=99)
        target = self.module.MigrationService(target_dir)
        old = (target_dir / "history" / "222.json").read_bytes()
        preview = target.preview_import(path)
        self.assertFalse(preview["canImport"])
        with self.assertRaises(ValueError):
            target.import_bundle(path, confirmation_token=preview["token"])
        self.assertEqual((target_dir / "history" / "222.json").read_bytes(), old)
        result = target.import_bundle(path, replace=True, confirmation_token=preview["token"])
        self.assertTrue(Path(result["backupPath"]).is_dir())
        self.assertEqual((Path(result["backupPath"]) / "history" / "222.json").read_bytes(), old)
        with closing(sqlite3.connect(target_dir / "daily.sqlite3")) as conn:
            self.assertEqual(conn.execute("SELECT price FROM daily_records").fetchone()[0], 12.5)
        self.assertTrue((target_dir / "history" / "111.json").exists())

    def test_preview_token_expires_if_target_changes(self):
        path = self._bundle()
        target = self._target()
        preview = target.preview_import(path)
        target.save_preferences({"version": 1, "profiles": [], "appearance": {"mode": "light"}, "dailyMapping": {}})
        with self.assertRaises(ValueError):
            target.import_bundle(path, confirmation_token=preview["token"])
        self.assertFalse((target.data_dir / "daily.sqlite3").exists())

    def test_preview_token_expires_if_bundle_changes(self):
        path = self._bundle()
        target = self._target()
        preview = target.preview_import(path)
        path.write_bytes(path.read_bytes() + b"changed")
        with self.assertRaises(ValueError):
            target.import_bundle(path, confirmation_token=preview["token"])

    def test_zip_traversal_absolute_paths_and_symlink_entries_are_rejected(self):
        path = self._bundle()
        for index, unsafe in enumerate(("../escape.json", "/tmp/escape.json", "history\\escape.json")):
            modified = self._rewrite_zip(path, f"unsafe-{index}.zip", lambda entries: entries + [(unsafe, b"{}", None)])
            with self.assertRaises(ValueError, msg=unsafe):
                self._target().preview_import(modified)
        symlink = zipfile.ZipInfo("history/999.json")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        modified = self._rewrite_zip(path, "symlink.zip", lambda entries: entries + [(symlink.filename, b"/outside", symlink)])
        with self.assertRaises(ValueError):
            self._target().preview_import(modified)
        self.assertFalse((self.root / "escape.json").exists())

    def test_duplicate_zip_entries_are_rejected(self):
        path = self._bundle()
        modified = self._rewrite_zip(path, "duplicate.zip", lambda entries: entries + [entries[0]])
        with self.assertRaises(ValueError):
            self._target().preview_import(modified)

    def test_wrong_hash_unsupported_version_and_corrupt_database_are_rejected(self):
        path = self._bundle()
        bad_hash = self._manifest_change(path, "hash.zip", lambda manifest: manifest["files"][0].update(sha256="0" * 64))
        with self.assertRaises(ValueError):
            self._target().preview_import(bad_hash)
        version = self._manifest_change(path, "version.zip", lambda manifest: manifest.update(formatVersion=999))
        with self.assertRaises(ValueError):
            self._target().preview_import(version)
        def corrupt_db(entries):
            manifest = json.loads(next(data for name, data, _ in entries if name == "manifest.json"))
            for item in manifest["files"]:
                if item["path"] == "daily.sqlite3":
                    item.update(bytes=7, sha256=hashlib.sha256(b"invalid").hexdigest())
            return [(name, b"invalid" if name == "daily.sqlite3" else json.dumps(manifest).encode() if name == "manifest.json" else data, info)
                    for name, data, info in entries]
        corrupted = self._rewrite_zip(path, "database.zip", corrupt_db)
        with self.assertRaises(ValueError):
            self._target().preview_import(corrupted)

    def test_import_failure_restores_existing_data_without_touching_external_files(self):
        path = self._bundle()
        target_dir = self.root / "existing"
        self._seed(target_dir, "222", price=99)
        target = self.module.MigrationService(target_dir)
        old_json = (target_dir / "history" / "222.json").read_bytes()
        outside = self.root / "do-not-change.xlsx"
        outside.write_bytes(b"user data")
        preview = target.preview_import(path)
        copy = target._atomic_copy
        failed = False
        def fail_once(source, destination):
            nonlocal failed
            if Path(destination) == target_dir / "history" / "111.json" and not failed:
                failed = True
                raise OSError("injected write failure")
            return copy(source, destination)
        with patch.object(target, "_atomic_copy", side_effect=fail_once):
            with self.assertRaises(ValueError):
                target.import_bundle(path, replace=True, confirmation_token=preview["token"])
        self.assertTrue(failed)
        self.assertEqual((target_dir / "history" / "222.json").read_bytes(), old_json)
        self.assertFalse((target_dir / "history" / "111.json").exists())
        with closing(sqlite3.connect(target_dir / "daily.sqlite3")) as conn:
            self.assertEqual(conn.execute("SELECT price FROM daily_records").fetchone()[0], 99)
        self.assertEqual(outside.read_bytes(), b"user data")
        self.assertIn("rolled_back", (target_dir / "logs" / "operations.jsonl").read_text())

    def test_history_id_conflicts_keep_local_status_in_merge_mode(self):
        # No incoming DB conflict: retain just one history record + preferences.
        path = self._bundle()
        target = self._target()
        (target.data_dir / "history_deleted").mkdir()
        local = target.data_dir / "history_deleted" / "111.json"
        local.write_text(json.dumps({"id": "111", "summary": {"marker": 99}, "products": []}))
        before = local.read_bytes()
        preview = target.preview_import(path)
        self.assertTrue(preview["conflicts"])
        target.import_bundle(path, confirmation_token=preview["token"])
        self.assertEqual(local.read_bytes(), before)
        self.assertFalse((target.data_dir / "history" / "111.json").exists())

    def test_preferences_reject_credentials_and_survive_export_import(self):
        with self.assertRaises(ValueError):
            self.service.save_preferences({"version": 1, "profiles": [], "appearance": {},
                                           "dailyMapping": {"authToken": "do-not-store"}})
        self.assertNotIn("do-not-store", (self.origin / "preferences.json").read_text())
        value = self.service.load_preferences()
        self.assertEqual(value["profiles"][0]["config"]["targetRate"], .85)


if __name__ == "__main__":
    unittest.main(verbosity=2)
