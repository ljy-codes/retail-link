"""Explicit, limited XLSX field patches. Ordinary exports never use this path."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import tempfile
import threading
import time
from zipfile import ZipFile
import xml.etree.ElementTree as ET

import openpyxl
from openpyxl.utils import column_index_from_string, get_column_letter

FIELDS = {"plan": "月计划", "stockSmall": "现货小单位", "stockAmount": "现货金额",
          "lossAmount": "登记货损金额", "supplyPrice": "供货单价", "price": "记录售价",
          "salesQuantity": "来源销量（未扣售后）"}
_LOCK = threading.RLock()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def has_user_symlink(path):
    for part in (Path(path), *Path(path).parents):
        if not part.is_symlink():
            continue
        # macOS root-owned system aliases are not user-selected file aliases.
        if str(part) in {"/var", "/tmp"} and part.lstat().st_uid == 0 and str(part.resolve()) in {"/private/var", "/private/tmp"}:
            continue
        return True
    return False


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".writeback-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _text(value):
    return "" if value is None else str(int(value)) if isinstance(value, float) and value.is_integer() else str(value).strip()


def _value(value):
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError("写回数值不能是布尔值。")
    try:
        number = float(value)
    except (ValueError, TypeError):
        raise ValueError("写回值需为非负数字或空白。") from None
    if not math.isfinite(number) or number < 0:
        raise ValueError("写回值需为非负有限数字。")
    return number


class SourceWriteback:
    def __init__(self, data_dir, protected_dirs=()):
        self.directory = Path(data_dir).resolve() / "writeback"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.protected_dirs = [Path(p).resolve() for p in protected_dirs]
        self.tokens = {}

    def _read(self, name, default):
        p = self.directory / name
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default

    def settings(self, enabled=None):
        with _LOCK:
            if enabled is not None:
                if not isinstance(enabled, bool):
                    raise ValueError("写回开关需为布尔值。")
                _atomic_json(self.directory / "settings.json", {"enabled": enabled})
                self.tokens.clear()
            return {"enabled": self._read("settings.json", {}).get("enabled") is True,
                    "fields": FIELDS, "templates": self._read("templates.json", {})}

    def _source(self, entry):
        path = Path(entry["path"]).expanduser()
        selected = Path(entry.get("selectedPath") or path).expanduser()
        if entry.get("selectedViaSymlink") or has_user_symlink(selected):
            raise ValueError("请直接选择原件，受控写回不接受符号链接路径。")
        if path.is_symlink() or path.suffix.lower() != ".xlsx" or not path.is_file():
            raise ValueError("受控写回只支持普通.xlsx文件，不支持链接或宏工作簿。")
        path = path.resolve()
        if any(path.is_relative_to(p) for p in self.protected_dirs) or any(path.is_relative_to(Path(p)) for p in ("/System", "/usr", "/bin", "/sbin")):
            raise ValueError("不能写回应用资源或系统目录。")
        if path.stat().st_nlink > 1:
            raise ValueError("原件存在硬链接，请先使用独立副本。")
        if (path.parent / ("~$" + path.name)).exists():
            raise ValueError("文件正在被Excel使用，请关闭后重新导入。")
        if path.stat().st_size > 30 * 1024 * 1024:
            raise ValueError("源文件过大，请使用30MB以内的限定模板。")
        if entry.get("sha256") and sha(path) != entry["sha256"]:
            raise ValueError("原件在导入后已变化，请重新选择文件。")
        with ZipFile(path) as archive:
            unsupported = ("xl/drawings/", "xl/externalLinks/", "xl/pivotTables/", "xl/embeddings/", "xl/activeX/")
            if any(n.startswith(unsupported) or "vbaproject" in n.lower() or n.startswith(("_xmlsignatures/", "customXml/")) for n in archive.namelist()):
                raise ValueError("首期写回不支持图片、图表、透视表、外部链接或嵌入对象，请另存结果。")
            ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            for part in ("xl/workbook.xml", "xl/styles.xml"):
                if part in archive.namelist() and any(e.tag.rsplit("}", 1)[-1] == "extLst" for e in ET.fromstring(archive.read(part)).iter()):
                    raise ValueError("原表含无法完整保留的Excel扩展，请另存结果。")
            for name in archive.namelist():
                if name.startswith("xl/worksheets/") and name.endswith(".xml"):
                    tree = ET.fromstring(archive.read(name))
                    if any(element.tag.rsplit("}", 1)[-1] == "extLst" for element in tree.iter()):
                        raise ValueError("原表含无法完整保留的Excel扩展，请使用简单限定模板或另存结果。")
                    for cell in tree.findall(".//s:c", ns):
                        cached = cell.find("s:v", ns)
                        if cell.find("s:f", ns) is not None and cached is not None and cached.text not in (None, ""):
                            raise ValueError("原表含公式计算缓存，首期写回不能保证其依赖重算，请使用无公式依赖的限定模板或另存结果。")
        return path

    @staticmethod
    def _schema(book, template):
        if template["sheet"] not in book.sheetnames:
            raise ValueError("目标工作表不存在。")
        sheet = book[template["sheet"]]
        headers = {col: _text(sheet[f"{col}{template['headerRow']}"].value)
                   for col in [*template["keyColumns"].values(), *template["fields"].values()]}
        return sheet, headers

    def register_template(self, entry, body):
        with _LOCK:
            path = self._source(entry)
            source_hash = sha(path)
            keys = body.get("keyColumns") or {"product": body.get("productColumn", "A")}
            fields = body.get("fields") or {}
            if not isinstance(keys, dict) or set(keys) - {"product", "specification", "warehouseId", "date"} or "product" not in keys:
                raise ValueError("模板需要商品标识列，可附加规格、仓库和日期列。")
            if not isinstance(fields, dict) or not fields or set(fields) - set(FIELDS) or len(fields) > 3:
                raise ValueError("请选择1至3个允许写回的数值字段。")
            columns = []
            for mapping in (keys, fields):
                for key, val in mapping.items():
                    col = str(val).strip().upper()
                    if not 1 <= column_index_from_string(col) <= 16384:
                        raise ValueError("模板列无效。")
                    mapping[key] = col
                    columns.append(col)
            if len(columns) != len(set(columns)):
                raise ValueError("商品键和写回字段不能使用同一列。")
            header = int(body.get("headerRow", 1))
            if not 1 <= header <= 1000:
                raise ValueError("表头行超出范围。")
            template = {"id": secrets.token_hex(12), "path": str(path), "sheet": str(body.get("sheet", "")),
                        "headerRow": header, "keyColumns": keys, "fields": fields, "createdAt": _now()}
            book = openpyxl.load_workbook(path, keep_links=False)
            try:
                sheet, headers = self._schema(book, template)
                if any(not v for v in headers.values()):
                    raise ValueError("所选列的表头不能为空。")
                template["headers"] = headers
                self._rows(sheet, template)
            finally:
                book.close()
            if sha(path) != source_hash:
                raise ValueError("原件在模板核对期间已变化，请重新导入。")
            templates = self._read("templates.json", {})
            templates[template["id"]] = template
            _atomic_json(self.directory / "templates.json", templates)
            return self.describe(entry, template["id"])

    def _template(self, identifier, path):
        template = self._read("templates.json", {}).get(str(identifier))
        if not template or template["path"] != str(path):
            raise ValueError("请选择该源文件对应的已确认模板。")
        return template

    def _rows(self, sheet, template):
        rows = []
        seen = set()
        numbers = sorted({c.row for c in sheet._cells.values() if c.value is not None and c.row > template["headerRow"]})
        if len(numbers) > 10000:
            raise ValueError("限定模板最多处理10000行，请缩小源表。")
        for row in numbers:
            keys = {k: _text(sheet[f"{v}{row}"].value) for k, v in template["keyColumns"].items()}
            if not keys["product"] or keys["product"] in {"合计", "小计", "总计"}:
                continue
            identity = tuple(keys.values())
            if identity in seen:
                raise ValueError("模板键存在重复商品，请增加规格、仓库或日期键后重新确认。")
            seen.add(identity)
            rows.append({"row": row, "keys": keys, "values": {k: sheet[f"{v}{row}"].value for k, v in template["fields"].items()}})
        return rows

    def describe(self, entry, identifier):
        path = self._source(entry)
        source_hash = sha(path)
        template = self._template(identifier, path)
        book = openpyxl.load_workbook(path, keep_links=False)
        try:
            sheet, headers = self._schema(book, template)
            if headers != template["headers"]:
                raise ValueError("原表表头已变化，请重新确认模板。")
            rows = self._rows(sheet, template)
            if sha(path) != source_hash:
                raise ValueError("原件在读取期间已变化，请重新导入。")
            return {"template": template, "rows": rows[:500], "total": len(rows), "sha256": source_hash}
        finally:
            book.close()

    def preview(self, entry, body):
        with _LOCK:
            if not self.settings()["enabled"]:
                raise ValueError("请先在设置中开启受控源写回。")
            path = self._source(entry)
            source_hash = sha(path)
            template = self._template(body.get("templateId"), path)
            changes = body.get("changes")
            if not isinstance(changes, list) or not 1 <= len(changes) <= 500:
                raise ValueError("每次请提交1至500条商品修正。")
            book = openpyxl.load_workbook(path, keep_links=False)
            try:
                sheet, headers = self._schema(book, template)
                if headers != template["headers"]:
                    raise ValueError("表头已变化，请重新确认模板。")
                rows = self._rows(sheet, template)
                by_key = {tuple(row["keys"].values()): row for row in rows}
                diffs, cells = [], set()
                for change in changes:
                    key = tuple(_text((change.get("keys") or {}).get(k)) for k in template["keyColumns"])
                    row = by_key.get(key)
                    if row is None:
                        raise ValueError("修正中的商品键未匹配原表，请重新核对。")
                    fields = change.get("fields") or {}
                    if not fields or set(fields) - set(template["fields"]):
                        raise ValueError("修正字段不在本模板允许范围。")
                    for field, value in fields.items():
                        cell = sheet[f"{template['fields'][field]}{row['row']}"]
                        if cell.coordinate in cells or cell.data_type == "f" or cell.__class__.__name__ == "MergedCell":
                            raise ValueError("目标单元格重复、合并或包含公式，不能写回。")
                        if any(cell.coordinate in merged for merged in sheet.merged_cells.ranges):
                            raise ValueError("首期不写入合并单元格。")
                        cells.add(cell.coordinate)
                        old, new = cell.value, _value(value)
                        if old is not None and not isinstance(old, (int, float)):
                            raise ValueError("目标需为数字或空白，不能覆盖文本、公式或单位说明。")
                        if old != new:
                            diffs.append({"sheet": sheet.title, "cell": cell.coordinate, "keys": row["keys"],
                                          "field": field, "label": FIELDS[field], "old": old, "new": new})
                if not diffs:
                    raise ValueError("没有需要写回的变化。")
            finally:
                book.close()
            if sha(path) != source_hash:
                raise ValueError("原件在差异预览期间已变化，请重新导入。")
            token = secrets.token_hex(24)
            preview = {"path": str(path), "filename": path.name, "sha256": source_hash, "changes": diffs,
                       "templateId": template["id"], "template": template, "created": time.monotonic()}
            self.tokens[token] = preview
            return {"token": token, **{k: v for k, v in preview.items() if k != "created"}, "backupRequired": True}

    def _copy(self, source, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            raise ValueError("备份目标不能是链接。")
        # Copy metadata only after flushing. Windows copied read-only attributes
        # make the target impossible to reopen for FlushFileBuffers.
        shutil.copyfile(source, target)
        with target.open("rb+") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        shutil.copystat(source, target)

    def commit(self, token, confirmed=False):
        with _LOCK:
            preview = self.tokens.get(str(token))
            if not confirmed or not self.settings()["enabled"] or not preview or time.monotonic() - preview["created"] > 600:
                raise ValueError("请开启写回并重新预览、确认本次差异。")
            path = self._source(preview)
            if sha(path) != preview["sha256"]:
                raise ValueError("预览后原件已变化，请重新导入。")
            if shutil.disk_usage(path.parent).free < path.stat().st_size * 3 + 1024 * 1024:
                raise ValueError("磁盘剩余空间不足，未写回原件。")
            identifier = secrets.token_hex(16)
            backup = self.directory / "backups" / f"{identifier}.xlsx"
            self._copy(path, backup)
            if sha(backup) != preview["sha256"]:
                raise ValueError("原件备份校验失败，未执行写回。")
            record = {"id": identifier, "path": str(path), "backup": str(backup), "backupSha256": sha(backup),
                      "beforeSha256": preview["sha256"], "changes": preview["changes"], "createdAt": _now(), "status": "prepared"}
            meta = self.directory / "operations" / f"{identifier}.json"
            _atomic_json(meta, record)
            fd, name = tempfile.mkstemp(prefix=".retail-patch-", suffix=".xlsx", dir=path.parent)
            os.close(fd)
            pending = Path(name)
            replaced = False
            try:
                book = openpyxl.load_workbook(backup, keep_links=False)
                before = {(s.title, c.coordinate): (c.value, c.style_id) for s in book for c in s._cells.values() if c.value is not None}
                template = preview["template"]
                sheet, headers = self._schema(book, template)
                if headers != template["headers"]:
                    book.close()
                    raise ValueError("备份模板结构与预览不一致，未执行写回。")
                for diff in preview["changes"]:
                    target = book[diff["sheet"]][diff["cell"]]
                    keys = {key: _text(sheet[f"{col}{target.row}"].value) for key, col in template["keyColumns"].items()}
                    if keys != diff["keys"] or target.value != diff["old"] or target.data_type == "f":
                        book.close()
                        raise ValueError("目标商品键或旧值与预览不一致，未执行写回。")
                    book[diff["sheet"]][diff["cell"]] = diff["new"]
                book.save(pending)
                book.close()
                with ZipFile(backup) as old_zip, ZipFile(pending) as new_zip:
                    lost = {name for name in set(old_zip.namelist()) - set(new_zip.namelist()) if not name.endswith("/")}
                    # Shared strings may legitimately become inline strings; cell values are checked below.
                    if lost - {"xl/sharedStrings.xml"}:
                        raise ValueError("限定模板含不能完整保留的工作簿内容，未修改原件。")
                expected = {k: value for k, (value, _) in before.items()}
                for diff in preview["changes"]:
                    expected[(diff["sheet"], diff["cell"])] = diff["new"]
                check = openpyxl.load_workbook(pending, keep_links=False)
                try:
                    actual = {(s.title, c.coordinate): c.value for s in check for c in s._cells.values() if c.value is not None}
                    if {k: v for k, v in expected.items() if v is not None} != actual:
                        raise ValueError("写回副本重读与预期不一致，原件未改动。")
                finally:
                    check.close()
                with pending.open("rb+") as stream:
                    stream.flush()
                    os.fsync(stream.fileno())
                record["afterSha256"] = sha(pending)
                _atomic_json(meta, record)
                self._source(preview)
                if sha(path) != preview["sha256"]:
                    raise ValueError("备份/生成期间原件已变化，拒绝写回。")
                pending.chmod(path.stat().st_mode & 0o777)
                os.replace(pending, path)
                replaced = True
                if sha(path) != record["afterSha256"]:
                    raise ValueError("发布后校验失败。")
                record["status"] = "success"
                _atomic_json(meta, record)
                self.tokens.pop(str(token), None)
                return record
            except Exception:
                if replaced and path.is_file() and sha(path) == record.get("afterSha256"):
                    self._copy(backup, pending)
                    os.replace(pending, path)
                    record["status"] = "auto_restored"
                    _atomic_json(meta, record)
                raise
            finally:
                pending.unlink(missing_ok=True)

    def history(self):
        directory = self.directory / "operations"
        records = [json.loads(p.read_text(encoding="utf-8")) for p in directory.glob("*.json")] if directory.exists() else []
        return {"items": sorted(records, key=lambda x: x["createdAt"], reverse=True)}

    def restore(self, identifier, confirmed=False):
        with _LOCK:
            if not confirmed or not isinstance(identifier, str) or len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier):
                raise ValueError("请确认要恢复的写回记录。")
            meta = self.directory / "operations" / f"{identifier}.json"
            record = json.loads(meta.read_text(encoding="utf-8"))
            path, backup = Path(record["path"]), Path(record["backup"])
            if backup.parent != self.directory / "backups" or backup.is_symlink() or sha(backup) != record["backupSha256"]:
                raise ValueError("备份已变化，拒绝恢复。")
            if record["status"] not in {"success", "prepared"} or path.is_symlink() or sha(path) != record.get("afterSha256"):
                raise ValueError("写回后原件已变化或记录已恢复，拒绝覆盖。")
            self._source({"path": str(path), "sha256": record["afterSha256"]})
            fd, name = tempfile.mkstemp(prefix=".retail-restore-", suffix=".xlsx", dir=path.parent)
            os.close(fd)
            pending = Path(name)
            try:
                self._copy(backup, pending)
                if sha(pending) != record["backupSha256"]:
                    raise ValueError("恢复副本与备份校验不一致。")
                if sha(path) != record["afterSha256"]:
                    raise ValueError("恢复前原件已变化。")
                os.replace(pending, path)
                record["status"] = "restored"
                record["restoredAt"] = _now()
                _atomic_json(meta, record)
                self.tokens.clear()
                return record
            finally:
                pending.unlink(missing_ok=True)
