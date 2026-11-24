"""Versioned local data bundles. Sources and generated workbooks are excluded.

Only whitelisted application files can enter a bundle. Imported audit records
are archived separately and never become executable export rollback records.
The HTTP host must hold its shared I/O lock while importing or mutating stores.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
import uuid
import zipfile

from .file_publish import publish_no_clobber
from .product_identity import CATALOG_COLUMNS as IDENTITY_CATALOG_COLUMNS, DAILY_IDENTITY_FIELDS, daily_identity, normalize_catalog


class MigrationError(ValueError):
    pass


class MigrationService:
    FORMAT = "retail-link-local-data"
    FORMAT_VERSION = 1
    MAX_BYTES = 256 * 1024 * 1024
    MAX_FILE_BYTES = 128 * 1024 * 1024
    MAX_FILES = 20000
    TOKEN_SECONDS = 600
    _LOCKS: dict[str, threading.RLock] = {}
    _REGISTRY_LOCK = threading.Lock()
    _HISTORY_RE = re.compile(r"(history|history_deleted)/([0-9]{1,64})\.json\Z", re.ASCII)
    _LOG_RE = re.compile(r"logs/(operations\.jsonl|imported/[a-zA-Z0-9_-]{1,100}\.jsonl)\Z", re.ASCII)
    _SECRET_RE = re.compile(r"password|passwd|cookie|token|api.?key|authorization|secret|credential", re.I)
    RECORD_COLUMNS = {"id", "date", "product", "name", "shop", "price", "salesQuantity", "source", "createdAt", "updatedAt", "deleted", "revision"}
    REVISION_COLUMNS = {"seq", "recordId", "savedAt", "action", "snapshot"}
    CATALOG_COLUMNS = {"sourceProduct", "sourceShop", "product", "name", "specification", "shop", "source", "revision", "updatedAt"}

    def __init__(self, data_dir, audit=None, app_version="0.1.2"):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.audit = audit
        self.app_version = str(app_version)
        self._tokens = {}
        with self._REGISTRY_LOCK:
            self._lock = self._LOCKS.setdefault(str(self.data_dir), threading.RLock())

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _sha(path):
        digest = hashlib.sha256()
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _json(value):
        try:
            return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
        except (TypeError, ValueError, OverflowError) as exc:
            raise MigrationError("数据包含无法保存的值。") from exc

    @classmethod
    def _read_json(cls, path):
        try:
            # Reject nonfinite JSON numbers even if another writer allowed them.
            return json.loads(Path(path).read_text(encoding="utf-8"),
                              parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (OSError, UnicodeError, ValueError) as exc:
            raise MigrationError("迁移数据的 JSON 内容无效。") from exc

    def _inside(self, relative):
        path = self.data_dir / relative
        for item in [path, *path.parents]:
            if item == self.data_dir.parent:
                break
            if item.is_symlink():
                raise MigrationError("应用数据路径包含符号链接，已停止迁移。")
        if not path.resolve().is_relative_to(self.data_dir):
            raise MigrationError("应用数据路径越界。")
        return path

    @staticmethod
    def _sync_directory(path):
        try:
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass

    def _atomic_copy(self, source, target):
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            fd, name = tempfile.mkstemp(prefix=".migration-", suffix=".tmp", dir=target.parent)
            temporary = Path(name)
            with os.fdopen(fd, "wb") as dest, Path(source).open("rb") as original:
                shutil.copyfileobj(original, dest)
                dest.flush()
                os.fsync(dest.fileno())
            os.replace(temporary, target)
            self._sync_directory(target.parent)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    def _audit(self, operation, **payload):
        safe = {"operationId": uuid.uuid4().hex, **payload}
        if self.audit:
            self.audit(operation, safe)
            return
        path = self._inside("logs/operations.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"operation": operation, "time": self._now(), "appVersion": self.app_version, **safe}
        with path.open("a", encoding="utf-8") as stream:
            stream.write(self._json(entry).decode().replace("\n", " ") + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    @classmethod
    def validate_preferences(cls, value):
        if not isinstance(value, dict) or value.get("version") != 1:
            raise MigrationError("配置文件版本不受支持。")
        if set(value) - {"version", "profiles", "appearance", "dailyMapping"}:
            raise MigrationError("配置文件含未知字段。")
        profiles = value.get("profiles", [])
        if not isinstance(profiles, list) or len(profiles) > 100:
            raise MigrationError("配置列表格式无效，最多保存 100 项。")
        for profile in profiles:
            if not isinstance(profile, dict) or not isinstance(profile.get("name"), str) or not isinstance(profile.get("config"), dict):
                raise MigrationError("分析配置缺少名称或配置内容。")
        if not isinstance(value.get("appearance", {}), dict) or (value.get("dailyMapping") is not None and not isinstance(value.get("dailyMapping"), dict)):
            raise MigrationError("外观或价格表映射配置格式无效。")

        def inspect(item):
            if isinstance(item, dict):
                for key, child in item.items():
                    if cls._SECRET_RE.search(str(key)):
                        raise MigrationError("配置不能包含密码、Cookie、Token 或其他凭据。")
                    inspect(child)
            elif isinstance(item, list):
                for child in item:
                    inspect(child)
        inspect(value)
        payload = cls._json(value)
        if len(payload) > 2 * 1024 * 1024:
            raise MigrationError("配置文件超过 2 MB。")
        return json.loads(payload)

    def save_preferences(self, value):
        with self._lock:
            prefs = self.validate_preferences(value)
            with self._staging() as directory:
                source = directory / "preferences.json"
                source.write_bytes(self._json(prefs))
                self._atomic_copy(source, self._inside("preferences.json"))
            self._audit("preferences_save", result="success", bytes=len(self._json(prefs)))
            return prefs

    def load_preferences(self):
        with self._lock:
            path = self._inside("preferences.json")
            if not path.is_file():
                return {"version": 1, "profiles": [], "appearance": {}, "dailyMapping": {}}
            return self.validate_preferences(self._read_json(path))

    @contextmanager
    def _staging(self):
        directory = self.data_dir / f"retail-migration-{uuid.uuid4().hex}"
        directory.mkdir(mode=0o777 if os.name == "nt" else 0o700)
        try:
            yield directory
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def _managed_files(self, logs=True):
        files = {}
        for folder in ("history", "history_deleted"):
            directory = self._inside(folder)
            if directory.exists():
                for path in directory.iterdir():
                    if not path.is_file() and not path.is_symlink():
                        continue
                    relative = path.relative_to(self.data_dir).as_posix()
                    if self._HISTORY_RE.fullmatch(relative):
                        files[relative] = self._inside(relative)
        for name in ("daily.sqlite3", "preferences.json", "import_templates.json", "operations/state.json"):
            path = self._inside(name)
            if path.is_file():
                files[name] = path
        if logs:
            directory = self._inside("logs")
            if directory.exists():
                for path in directory.rglob("*.jsonl"):
                    relative = path.relative_to(self.data_dir).as_posix()
                    if self._LOG_RE.fullmatch(relative):
                        files[relative] = self._inside(relative)
        return files

    def _fingerprint(self):
        files = self._managed_files(logs=False)
        for name in ("daily.sqlite3-wal", "daily.sqlite3-shm"):
            path = self._inside(name)
            if path.is_file():
                files[name] = path
        return {name: self._sha(path) for name, path in sorted(files.items())}

    @classmethod
    def _validate_database(cls, path):
        try:
            conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            try:
                conn.execute("PRAGMA query_only=ON")
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1, 2):
                    raise MigrationError("日记录数据库版本过新，不能导入当前工具。")
                if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise MigrationError("日记录数据库完整性校验失败。")
                objects = conn.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
                if any(kind not in ("table", "index") or (kind == "table" and name not in ("daily_records", "daily_revisions", "product_catalog")) for kind, name in objects):
                    raise MigrationError("日记录数据库含不支持的结构。")
                record_columns = cls.RECORD_COLUMNS | set(DAILY_IDENTITY_FIELDS) | {"identityKey"} if version == 2 else cls.RECORD_COLUMNS
                for table, expected in (("daily_records", record_columns), ("daily_revisions", cls.REVISION_COLUMNS)):
                    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                    if columns != expected:
                        raise MigrationError("日记录数据库字段不兼容。")
                for row in conn.execute("SELECT id,date,product,price,salesQuantity,deleted,revision,source FROM daily_records"):
                    ident, day, product, price, qty, deleted, revision, source = row
                    if not re.fullmatch(r"[0-9a-f]{32}", str(ident)) or date.fromisoformat(day).isoformat() != day or not product:
                        raise MigrationError("日记录数据库含无效标识或日期。")
                    if any(v is not None and (not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0) for v in (price, qty)):
                        raise MigrationError("日记录数据库含无效数值。")
                    if deleted not in (0, 1) or not isinstance(revision, int) or revision < 1:
                        raise MigrationError("日记录数据库状态无效。")
                    json.loads(source, parse_constant=lambda v: (_ for _ in ()).throw(ValueError(v)))
                if version == 2:
                    conn.row_factory = sqlite3.Row
                    for row in conn.execute("SELECT * FROM daily_records"):
                        record = dict(row)
                        record["source"] = json.loads(record["source"])
                        if any(not isinstance(record[key], str) or len(record[key]) > 200 for key in DAILY_IDENTITY_FIELDS) or record["identityKey"] != daily_identity(record):
                            raise MigrationError("日记录身份字段或去重键无效。")
                    conn.row_factory = None
                for record_id, snapshot in conn.execute("SELECT recordId,snapshot FROM daily_revisions"):
                    saved = json.loads(snapshot)
                    if not isinstance(saved, dict) or saved.get("id") != record_id:
                        raise MigrationError("日记录旧版本内容无效。")
                catalog_count = 0
                if ("table", "product_catalog") in objects:
                    columns = {row[1] for row in conn.execute("PRAGMA table_info(product_catalog)")}
                    expected_catalog = IDENTITY_CATALOG_COLUMNS if version == 2 else cls.CATALOG_COLUMNS
                    if columns != expected_catalog:
                        raise MigrationError("商品关联字段不兼容。")
                    keys = set()
                    conn.row_factory = sqlite3.Row
                    for row in conn.execute("SELECT * FROM product_catalog"):
                        item = dict(row)
                        source_product, source_shop, product, name, specification, shop, source, revision, updated = [item[key] for key in ("sourceProduct", "sourceShop", "product", "name", "specification", "shop", "source", "revision", "updatedAt")]
                        if (not isinstance(source_product, str) or not source_product.strip() or len(source_product) > 200
                                or not isinstance(product, str) or not product.strip() or len(product) > 200
                                or any(not isinstance(v, str) or len(v) > 500 for v in (source_shop, name, specification, shop))
                                or not isinstance(revision, int) or revision < 1):
                            raise MigrationError("商品关联内容无效。")
                        key = (source_product, source_shop)
                        if version == 2:
                            normalized = normalize_catalog(item | {"source": json.loads(source)})
                            if normalized["catalogId"] != item["catalogId"] or any(not isinstance(item[k], str) for k in IDENTITY_CATALOG_COLUMNS - {"revision"}):
                                raise MigrationError("商品关联身份或键无效。")
                            key = item["catalogId"]
                        if key in keys:
                            raise MigrationError("商品关联含重复来源。")
                        keys.add(key)
                        json.loads(source)
                        datetime.fromisoformat(updated)
                        catalog_count += 1
                    conn.row_factory = None
                return {"dailyRecords": conn.execute("SELECT COUNT(*) FROM daily_records").fetchone()[0],
                        "dailyRevisions": conn.execute("SELECT COUNT(*) FROM daily_revisions").fetchone()[0],
                        "productCatalog": catalog_count}
            finally:
                conn.close()
        except MigrationError:
            raise
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise MigrationError("日记录数据库无法校验。") from exc

    @staticmethod
    def _sqlite_backup(source, target):
        original = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=15)
        backup = sqlite3.connect(target)
        try:
            original.backup(backup)
            backup.commit()
        finally:
            backup.close()
            original.close()

    def _validate_files(self, files):
        counts = {"history": 0, "historyDeleted": 0, "dailyRecords": 0, "dailyRevisions": 0, "productCatalog": 0, "preferences": 0, "importTemplates": 0, "logFiles": 0, "operationRecords": 0}
        ids = set()
        for name, path in files.items():
            match = self._HISTORY_RE.fullmatch(name)
            if match:
                content = self._read_json(path)
                if not isinstance(content, dict) or str(content.get("id")) != match[2] or match[2] in ids:
                    raise MigrationError("历史快照 ID 无效或在正常与已删除列表中重复。")
                if not isinstance(content.get("products", []), list) or not isinstance(content.get("summary", {}), dict):
                    raise MigrationError("历史快照结构无效。")
                ids.add(match[2])
                counts["history" if match[1] == "history" else "historyDeleted"] += 1
            elif name == "daily.sqlite3":
                counts.update(self._validate_database(path))
            elif name == "operations/state.json":
                from .operations import validate_state
                try:
                    value = validate_state(self._read_json(path))
                except (ValueError, TypeError, KeyError) as exc:
                    raise MigrationError("经营记录结构无效，不能迁移。") from exc
                counts["operationRecords"] = len(value["records"])
            elif name == "preferences.json":
                self.validate_preferences(self._read_json(path))
                counts["preferences"] = 1
            elif name == "import_templates.json":
                value = self._read_json(path)
                if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("templates"), list):
                    raise MigrationError("导入格式模板结构无效。")
                template_ids = set()
                for template in value["templates"]:
                    if (not isinstance(template, dict) or not isinstance(template.get("id"), str)
                            or not re.fullmatch(r"[a-f0-9]{32}", template["id"])
                            or template["id"] in template_ids or not isinstance(template.get("name"), str)
                            or not 1 <= len(template["name"]) <= 80
                            or not isinstance(template.get("structures"), dict)
                            or not isinstance(template.get("selections"), dict)):
                        raise MigrationError("导入格式模板内容无效。")
                    template_ids.add(template["id"])
                counts["importTemplates"] = len(template_ids)
            elif self._LOG_RE.fullmatch(name):
                with path.open(encoding="utf-8") as stream:
                    for line in stream:
                        if line.strip() and not isinstance(json.loads(line), dict):
                            raise MigrationError("归档操作日志格式无效。")
                counts["logFiles"] += 1
            else:
                raise MigrationError("迁移包含不支持的文件。")
        return counts

    def _snapshot(self, directory, include_logs=True):
        files = self._managed_files(logs=include_logs)
        copied = {}
        for name, source in files.items():
            target = directory / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if name == "daily.sqlite3":
                self._sqlite_backup(source, target)
            else:
                shutil.copy2(source, target)
            copied[name] = target
        return copied

    def export_bundle(self, output_path):
        with self._lock:
            out = Path(output_path).expanduser()
            if out.suffix.lower() != ".zip" or not out.name or not out.parent.is_dir():
                raise MigrationError("请选择已存在目录中的 .zip 文件名。")
            if out.is_symlink() or out.exists():
                raise FileExistsError("迁移包文件已存在，请更换文件名。")
            out = out.resolve()
            temporary = None
            try:
                with self._staging() as staging:
                    files = self._snapshot(staging)
                    counts = self._validate_files(files)
                    created = self._now()
                    manifest = {"format": self.FORMAT, "formatVersion": self.FORMAT_VERSION,
                                "appVersion": self.app_version, "createdAt": created, "counts": counts,
                                "files": [{"path": name, "bytes": path.stat().st_size, "sha256": self._sha(path)} for name, path in sorted(files.items())]}
                    fd, name = tempfile.mkstemp(prefix=".retail-bundle-", suffix=".tmp", dir=out.parent)
                    os.close(fd)
                    temporary = Path(name)
                    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
                        for name, path in files.items():
                            archive.write(path, name)
                        archive.writestr("manifest.json", self._json(manifest))
                    with temporary.open("rb+") as stream:
                        os.fsync(stream.fileno())
                    # Exercise the same parser the receiving installation uses.
                    with self._validated_bundle(temporary):
                        pass
                    publish_no_clobber(temporary, out)
                    self._sync_directory(out.parent)
                    digest, size = self._sha(out), out.stat().st_size
                    self._audit("data_export", result="success", target=str(out), sha256=digest, bytes=size, counts=counts)
                    return {"path": str(out), "bytes": size, "sha256": digest, "createdAt": created, "counts": counts}
            except Exception as exc:
                self._audit("data_export", result="failed", error=type(exc).__name__)
                raise
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)

    @contextmanager
    def _validated_bundle(self, path):
        path = Path(path).expanduser().resolve()
        if not path.is_file() or path.stat().st_size > self.MAX_BYTES:
            raise MigrationError("迁移包不存在或超过 256 MB。")
        try:
            with zipfile.ZipFile(path) as archive, self._staging() as staging:
                infos = archive.infolist()
                names = [item.filename for item in infos]
                if len(infos) > self.MAX_FILES or len(names) != len(set(names)) or "manifest.json" not in names:
                    raise MigrationError("迁移包重复文件、数量或清单无效。")
                if sum(item.file_size for item in infos) > self.MAX_BYTES or any(item.file_size > self.MAX_FILE_BYTES for item in infos):
                    raise MigrationError("迁移包展开后超过大小限制。")
                for item in infos:
                    posix = PurePosixPath(item.filename)
                    if item.is_dir() or posix.is_absolute() or ".." in posix.parts or "\\" in item.filename or posix.as_posix() != item.filename or stat.S_ISLNK(item.external_attr >> 16) or item.flag_bits & 1:
                        raise MigrationError("迁移包包含不安全路径或不支持的文件。")
                    if item.filename != "manifest.json" and not (self._HISTORY_RE.fullmatch(item.filename) or self._LOG_RE.fullmatch(item.filename) or item.filename in ("daily.sqlite3", "preferences.json", "import_templates.json", "operations/state.json")):
                        raise MigrationError("迁移包包含不允许的文件。")
                if archive.getinfo("manifest.json").file_size > 8 * 1024 * 1024:
                    raise MigrationError("迁移包清单过大。")
                manifest = json.loads(archive.read("manifest.json"))
                if not isinstance(manifest, dict) or manifest.get("format") != self.FORMAT or manifest.get("formatVersion") != self.FORMAT_VERSION or not isinstance(manifest.get("files"), list):
                    raise MigrationError("迁移包版本不受支持。")
                entries = manifest["files"]
                if any(not isinstance(item, dict) for item in entries):
                    raise MigrationError("迁移包清单条目无效。")
                listed = [item.get("path") for item in entries]
                if len(listed) != len(set(listed)) or set(listed) != set(names) - {"manifest.json"}:
                    raise MigrationError("迁移包清单与实际文件不一致。")
                files = {}
                for item in entries:
                    name = item["path"]
                    info = archive.getinfo(name)
                    if item.get("bytes") != info.file_size or not re.fullmatch(r"[a-f0-9]{64}", str(item.get("sha256", ""))):
                        raise MigrationError("迁移包文件大小或校验码无效。")
                    destination = staging / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(name) as source, destination.open("wb") as target:
                        shutil.copyfileobj(source, target)
                    if self._sha(destination) != item["sha256"]:
                        raise MigrationError("迁移包文件校验失败。")
                    files[name] = destination
                counts = self._validate_files(files)
                yield manifest, files, counts
        except MigrationError:
            raise
        except (zipfile.BadZipFile, OSError, ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise MigrationError("迁移包无效或内容校验失败。") from exc

    def _conflicts(self, files):
        current = self._managed_files(logs=False)
        issues, blockers = [], []
        for name, source in files.items():
            if name.startswith("logs/"):
                continue
            target = current.get(name)
            cross = None
            if name.startswith("history/"):
                cross = current.get(name.replace("history/", "history_deleted/", 1))
            elif name.startswith("history_deleted/"):
                cross = current.get(name.replace("history_deleted/", "history/", 1))
            if cross:
                issues.append(f"{name} 的 ID 已在另一历史列表中；合并时保留本地状态。")
            elif target:
                issues.append(f"{name} 已存在；合并时保留本地文件。")
                if name in {"daily.sqlite3", "operations/state.json"} and self._sha(target) != self._sha(source):
                    blockers.append("本地已有日记录数据库或经营记录。请确认后选择替换，避免静默丢弃任何一侧记录。")
        return issues, blockers

    def preview_import(self, bundle_path):
        with self._lock:
            path = Path(bundle_path).expanduser().resolve()
            with self._validated_bundle(path) as (manifest, files, counts):
                conflicts, blockers = self._conflicts(files)
                digest = self._sha(path)
            token = uuid.uuid4().hex
            self._tokens = {key: value for key, value in self._tokens.items() if value["expires"] > time.monotonic()}
            self._tokens[token] = {"path": str(path), "sha256": digest, "fingerprint": self._fingerprint(),
                                   "expires": time.monotonic() + self.TOKEN_SECONDS}
            return {"token": token, "sha256": digest, "counts": counts, "conflicts": conflicts,
                    "canImport": not blockers, "requiresReplace": bool(blockers), "blockers": blockers,
                    "formatVersion": manifest["formatVersion"]}

    def import_bundle(self, bundle_path, replace=False, confirmation_token=None):
        with self._lock:
            token = self._tokens.get(str(confirmation_token))
            path = Path(bundle_path).expanduser().resolve()
            if not token or token["expires"] <= time.monotonic() or token["path"] != str(path):
                raise MigrationError("迁移确认已失效，请先重新预览。")
            if token["sha256"] != self._sha(path) or token["fingerprint"] != self._fingerprint():
                raise MigrationError("迁移包或本地数据在预览后变化，请重新预览。")
            if not isinstance(replace, bool):
                raise MigrationError("替换选项无效。")
            with self._validated_bundle(path) as (_, files, counts):
                conflicts, blockers = self._conflicts(files)
                if blockers and not replace:
                    raise MigrationError(blockers[0])
                operation_id = uuid.uuid4().hex
                backup_dir = self._inside("migration_backups/" + operation_id)
                backup_dir.mkdir(parents=True)
                old_files = self._snapshot(backup_dir, include_logs=False)
                old_names = set(old_files)
                critical = {name: file for name, file in files.items() if not name.startswith("logs/")}
                current = self._managed_files(logs=False)
                selected = {}
                skipped = 0
                for name, file in critical.items():
                    counterpart = name.replace("history/", "history_deleted/", 1) if name.startswith("history/") else name.replace("history_deleted/", "history/", 1) if name.startswith("history_deleted/") else name
                    if not replace and (name in current or counterpart in current):
                        skipped += 1
                    else:
                        selected[name] = file
                touched = set(selected) | (set(current) if replace else set())
                imported_log = None
                try:
                    if "daily.sqlite3" in touched:
                        for suffix in ("-wal", "-shm"):
                            self._inside("daily.sqlite3" + suffix).unlink(missing_ok=True)
                    if replace:
                        for name in set(current) - set(critical):
                            self._inside(name).unlink(missing_ok=True)
                    for name, file in selected.items():
                        self._atomic_copy(file, self._inside(name))
                    # Untrusted historical audit records are data only. They must
                    # never be read by the current export rollback service.
                    incoming_logs = [file for name, file in sorted(files.items()) if name.startswith("logs/")]
                    if incoming_logs:
                        imported_log = self._inside(f"logs/imported/{operation_id}.jsonl")
                        imported_log.parent.mkdir(parents=True, exist_ok=True)
                        with imported_log.open("x", encoding="utf-8") as output:
                            for file in incoming_logs:
                                with file.open(encoding="utf-8") as source:
                                    for line in source:
                                        if line.strip():
                                            output.write(line.rstrip() + "\n")
                            output.flush()
                            os.fsync(output.fileno())
                    final_counts = self._validate_files(self._managed_files(logs=False))
                    preferences = self.load_preferences()
                    self._audit("data_import", operationId=operation_id, source=str(path), sha256=token["sha256"],
                                result="success", replaced=replace, counts=counts, skipped=skipped)
                except Exception as exc:
                    for name in touched:
                        self._inside(name).unlink(missing_ok=True)
                    for suffix in ("-wal", "-shm"):
                        self._inside("daily.sqlite3" + suffix).unlink(missing_ok=True)
                    for name in old_names:
                        self._atomic_copy(old_files[name], self._inside(name))
                    if imported_log:
                        imported_log.unlink(missing_ok=True)
                    self._audit("data_import", operationId=operation_id, source=str(path),
                                result="rolled_back", error=type(exc).__name__)
                    raise MigrationError("导入未完成，已恢复导入前的本地数据。") from exc
                self._tokens.pop(str(confirmation_token), None)
                return {"result": "success", "counts": counts, "currentCounts": final_counts,
                        "backupPath": str(backup_dir), "operationId": operation_id, "preferences": preferences,
                        "skipped": skipped, "conflicts": conflicts, "replace": replace}
