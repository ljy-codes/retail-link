"""Dated price and sales observations, independent from workbook report snapshots."""
from __future__ import annotations

from datetime import date, datetime, timezone, timedelta
from contextlib import contextmanager
import json
import math
from pathlib import Path
import re
import sqlite3
import uuid

import openpyxl
from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.utils.datetime import from_excel

from .product_identity import DAILY_IDENTITY_FIELDS, daily_identity, normalize_catalog


FIELDS = ("date", "product", "name", "shop", "price", "salesQuantity", *DAILY_IDENTITY_FIELDS)
LABELS = {"date": "业务日期", "product": "商品标识", "name": "商品名称", "shop": "店铺／仓库", "price": "当日售价", "salesQuantity": "当日销量", "sourceSystem": "来源系统", "sourceSku": "来源SKU", "specification": "规格", "warehouseId": "仓库ID"}


def _text(value):
    return str(value).strip() if value is not None else ""


def _number(value, field):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{LABELS[field]}不能是布尔值。")
    if isinstance(value, str):
        value = re.sub(r"[,，￥¥\s]", "", value)
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{LABELS[field]}应为数字，或留空表示未知。") from None
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{LABELS[field]}应为大于等于零的有效数字。")
    return result


def _date(value, epoch=None):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)) and not isinstance(value, bool) and epoch is not None and 30000 < value < 80000:
        return from_excel(value, epoch).date().isoformat()
    text = _text(value)
    match = re.fullmatch(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})日?", text)
    if not match:
        match = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", text)
    if match:
        try:
            return date(*map(int, match.groups())).isoformat()
        except ValueError:
            pass
    raise ValueError("业务日期需包含完整年月日，例如 2026-11-21。")


def _normalized(record):
    if not isinstance(record, dict):
        raise ValueError("日记录应为对象。")
    product = _text(record.get("product"))
    if not product:
        raise ValueError("请填写商品标识。")
    if len(product) > 200:
        raise ValueError("商品标识过长。")
    result = {"date": _date(record.get("date")), "product": product,
              "name": _text(record.get("name")) or product, "shop": _text(record.get("shop")),
              "price": _number(record.get("price"), "price"),
              "salesQuantity": _number(record.get("salesQuantity"), "salesQuantity"),
              "source": record.get("source") or "手工录入"}
    result.update({key: _text(record.get(key)) for key in DAILY_IDENTITY_FIELDS})
    if any(len(result[key]) > 200 for key in DAILY_IDENTITY_FIELDS):
        raise ValueError("商品或仓库身份字段过长。")
    result["identityKey"] = daily_identity(result)
    if result["date"] > datetime.now(timezone(timedelta(hours=8))).date().isoformat():
        raise ValueError("业务日期在未来。当前入口记录已发生的每日售价与销量，计划价格请待生效后记录。")
    if result["price"] is None and result["salesQuantity"] is None:
        raise ValueError("售价和销量至少填写一项；缺少的一项可以留空。")
    if len(result["name"]) > 500 or len(result["shop"]) > 200:
        raise ValueError("商品名称或店铺名称过长。")
    json.dumps(result["source"], ensure_ascii=False, allow_nan=False)
    return result


class DailyStore:
    """One dated observation per product and location, with non-destructive edits."""

    def __init__(self, data_dir):
        directory = Path(data_dir).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "daily.sqlite3"
        with self._connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise ValueError("日记录数据库版本过新，请使用相应版本工具。")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""CREATE TABLE IF NOT EXISTS daily_revisions (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, recordId TEXT NOT NULL,
                savedAt TEXT NOT NULL, action TEXT NOT NULL, snapshot TEXT NOT NULL)""")
            old_columns = {row[1] for row in connection.execute("PRAGMA table_info(daily_records)")}
            if old_columns and "identityKey" not in old_columns:
                connection.execute("ALTER TABLE daily_records RENAME TO daily_records_legacy")
            connection.execute("""CREATE TABLE IF NOT EXISTS daily_records (
                id TEXT PRIMARY KEY, date TEXT NOT NULL, product TEXT NOT NULL,
                name TEXT NOT NULL, shop TEXT NOT NULL DEFAULT '', price REAL,
                salesQuantity REAL, source TEXT NOT NULL, createdAt TEXT NOT NULL,
                updatedAt TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 1,
                sourceSystem TEXT NOT NULL DEFAULT '', sourceSku TEXT NOT NULL DEFAULT '',
                specification TEXT NOT NULL DEFAULT '', warehouseId TEXT NOT NULL DEFAULT '',
                identityKey TEXT NOT NULL UNIQUE)""")
            if old_columns and "identityKey" not in old_columns:
                for row in connection.execute("SELECT * FROM daily_records_legacy").fetchall():
                    item = dict(row)
                    item.update({key: "" for key in DAILY_IDENTITY_FIELDS})
                    item["identityKey"] = daily_identity(item)
                    keys = list(item)
                    connection.execute("INSERT INTO daily_records(" + ",".join(keys) + ") VALUES(" + ",".join("?" for _ in keys) + ")", [item[key] for key in keys])
                connection.execute("DROP TABLE daily_records_legacy")
            connection.execute("CREATE INDEX IF NOT EXISTS daily_date ON daily_records(deleted,date)")
            catalog_columns = {row[1] for row in connection.execute("PRAGMA table_info(product_catalog)")}
            if catalog_columns and "catalogId" not in catalog_columns:
                connection.execute("ALTER TABLE product_catalog RENAME TO product_catalog_legacy")
            connection.execute("""CREATE TABLE IF NOT EXISTS product_catalog (
                catalogId TEXT PRIMARY KEY, sourceProduct TEXT NOT NULL, sourceShop TEXT NOT NULL DEFAULT '',
                product TEXT NOT NULL, name TEXT NOT NULL, specification TEXT NOT NULL DEFAULT '',
                shop TEXT NOT NULL DEFAULT '', sourceSystem TEXT NOT NULL DEFAULT '',
                sourceSku TEXT NOT NULL DEFAULT '', sourceSpecification TEXT NOT NULL DEFAULT '',
                sourceWarehouseId TEXT NOT NULL DEFAULT '', warehouseId TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, updatedAt TEXT NOT NULL)""")
            if catalog_columns and "catalogId" not in catalog_columns:
                for row in connection.execute("SELECT * FROM product_catalog_legacy").fetchall():
                    old = dict(row)
                    item = normalize_catalog(old | {"source": json.loads(old["source"])})
                    item.update(source=old["source"], revision=old["revision"], updatedAt=old["updatedAt"])
                    keys = list(item)
                    connection.execute("INSERT INTO product_catalog(" + ",".join(keys) + ") VALUES(" + ",".join("?" for _ in keys) + ")", [item[key] for key in keys])
                connection.execute("DROP TABLE product_catalog_legacy")
            connection.execute("PRAGMA user_version=2")

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=20)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _id(value):
        value = _text(value)
        if not re.fullmatch(r"[a-f0-9]{32}", value):
            raise ValueError("日记录标识无效。")
        return value

    @staticmethod
    def _row(row):
        if row is None:
            return None
        result = dict(row)
        result["source"] = json.loads(result["source"])
        result["deleted"] = bool(result["deleted"])
        for key in DAILY_IDENTITY_FIELDS:
            result.setdefault(key, "")
        result.setdefault("identityKey", daily_identity(result))
        return result

    def list(self, start=None, end=None, product="", shop="", deleted=False, offset=0, limit=500):
        conditions = ["deleted = ?"]
        parameters = [int(deleted)]
        if start:
            conditions.append("date >= ?")
            parameters.append(_date(start))
        if end:
            conditions.append("date <= ?")
            parameters.append(_date(end))
        if start and end and _date(start) > _date(end):
            raise ValueError("开始日期不能晚于结束日期。")
        if product:
            conditions.append("(instr(product, ?) > 0 OR instr(name, ?) > 0)")
            parameters.extend([str(product), str(product)])
        if shop:
            conditions.append("instr(shop, ?) > 0")
            parameters.append(str(shop))
        offset = max(0, int(offset))
        limit = min(1000, max(1, int(limit)))
        where = " AND ".join(conditions)
        with self._connect() as connection:
            total = connection.execute("SELECT COUNT(*) FROM daily_records WHERE " + where, parameters).fetchone()[0]
            rows = connection.execute("SELECT * FROM daily_records WHERE " + where + " ORDER BY date DESC, product, shop LIMIT ? OFFSET ?", parameters + [limit, offset]).fetchall()
        return {"items": [self._row(row) for row in rows], "total": total, "offset": offset, "limit": limit, "hasMore": offset + len(rows) < total}

    def get(self, identifier):
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM daily_records WHERE id = ?", [self._id(identifier)]).fetchone()
        if row is None:
            raise ValueError("日记录不存在。")
        return self._row(row)

    @staticmethod
    def _archive(connection, row, action):
        connection.execute("INSERT INTO daily_revisions(recordId,savedAt,action,snapshot) VALUES(?,?,?,?)",
                           [row["id"], datetime.now(timezone.utc).isoformat(), action,
                            json.dumps(dict(row), ensure_ascii=False, allow_nan=False)])

    def _save(self, connection, normalized, on_conflict="skip"):
        if on_conflict not in {"skip", "replace", "merge"}:
            raise ValueError("重复处理方式仅支持 skip、merge 或 replace。")
        existing = connection.execute("SELECT * FROM daily_records WHERE identityKey=?", [normalized["identityKey"]]).fetchone()
        if existing is not None:
            if on_conflict == "skip" or existing["deleted"]:
                return {"record": self._row(existing), "status": "skipped", "reason": "已有已删除记录，请先恢复。" if existing["deleted"] else "同日同商品已有记录，默认保留原值。"}
            if all(normalized[field] == existing[field] for field in ("name", "shop", "price", "salesQuantity")):
                return {"record": self._row(existing), "status": "skipped", "reason": "内容未变化，不生成重复版本。"}
            if on_conflict == "merge":
                normalized = dict(normalized)
                for field in ("price", "salesQuantity"):
                    old, new = existing[field], normalized[field]
                    if old is not None and new is not None and not math.isclose(old, new, rel_tol=1e-9, abs_tol=1e-9):
                        raise ValueError(f"{normalized['date']} 商品 {normalized['product']} 已有{LABELS[field]}与导入值不同；请核对后选择更新，补齐模式不会覆盖已有值。")
                    normalized[field] = old if old is not None else new
                if all(normalized[field] == existing[field] for field in ("price", "salesQuantity")):
                    return {"record": self._row(existing), "status": "skipped", "reason": "已有数值完整，无需补齐。"}
                normalized["name"] = existing["name"]
                normalized["source"] = {"mode": "补齐缺失字段", "previous": json.loads(existing["source"]), "incoming": normalized["source"]}
            self._archive(connection, existing, on_conflict)
            updated = datetime.now(timezone.utc).isoformat()
            connection.execute("UPDATE daily_records SET name=?,shop=?,price=?,salesQuantity=?,source=?,updatedAt=?,revision=revision+1 WHERE id=?",
                               [normalized["name"], normalized["shop"], normalized["price"], normalized["salesQuantity"], json.dumps(normalized["source"], ensure_ascii=False), updated, existing["id"]])
            identifier, status = existing["id"], "updated"
        else:
            identifier = uuid.uuid4().hex
            now = datetime.now(timezone.utc).isoformat()
            connection.execute("INSERT INTO daily_records(id,date,product,name,shop,price,salesQuantity,source,createdAt,updatedAt,sourceSystem,sourceSku,specification,warehouseId,identityKey) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               [identifier, normalized["date"], normalized["product"], normalized["name"], normalized["shop"], normalized["price"], normalized["salesQuantity"], json.dumps(normalized["source"], ensure_ascii=False), now, now] + [normalized[key] for key in (*DAILY_IDENTITY_FIELDS, "identityKey")])
            status = "created"
        row = connection.execute("SELECT * FROM daily_records WHERE id=?", [identifier]).fetchone()
        return {"record": self._row(row), "status": status}

    def save(self, record, on_conflict="skip"):
        normalized = _normalized(record)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._save(connection, normalized, on_conflict)

    def save_many(self, records, on_conflict="skip"):
        normalized = [_normalized(record) for record in records]
        counts = {"saved": 0, "skipped": 0, "replaced": 0}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for item in normalized:
                result = self._save(connection, item, on_conflict)
                counts[{"created": "saved", "skipped": "skipped", "updated": "replaced"}[result["status"]]] += 1
        return counts

    def set_deleted(self, identifier, deleted):
        identifier = self._id(identifier)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM daily_records WHERE id=?", [identifier]).fetchone()
            if row is None:
                raise ValueError("日记录不存在。")
            if bool(row["deleted"]) != bool(deleted):
                self._archive(connection, row, "delete" if deleted else "restore")
                connection.execute("UPDATE daily_records SET deleted=?,updatedAt=?,revision=revision+1 WHERE id=?", [int(deleted), datetime.now(timezone.utc).isoformat(), identifier])
        return {"id": identifier, "status": "deleted" if deleted else "restored"}

    def revisions(self, identifier):
        identifier = self._id(identifier)
        self.get(identifier)
        with self._connect() as connection:
            rows = connection.execute("SELECT savedAt,action,snapshot FROM daily_revisions WHERE recordId=? ORDER BY seq DESC", [identifier]).fetchall()
        return {"items": [{"savedAt": row["savedAt"], "action": row["action"], "record": self._row(json.loads(row["snapshot"]))} for row in rows]}

    def existing_keys(self):
        with self._connect() as connection:
            return {(row[0], row[1], row[2]) for row in connection.execute("SELECT date,product,shop FROM daily_records")}

    def existing_identity_keys(self):
        with self._connect() as connection:
            return {row[0] for row in connection.execute("SELECT identityKey FROM daily_records")}

    def reviewed_import(self, records, on_conflict="skip", preview_only=True, expected_fingerprint=None,
                        require_fingerprint=False, context=None):
        from .daily_import import review_records
        if on_conflict not in {"skip", "merge", "replace"}:
            raise ValueError("重复处理方式仅支持 skip、merge 或 replace。")
        if not preview_only and require_fingerprint and not expected_fingerprint:
            raise ValueError("请先预览导入结果，再携带预览指纹确认保存。")
        normalized = [_normalized(record) for record in records]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = review_records(connection, normalized, on_conflict, context)
            if expected_fingerprint and expected_fingerprint != result["previewFingerprint"]:
                raise ValueError("预览后的记录或商品对照已变化，请重新预览后保存。")
            if not preview_only:
                counts = {"saved": 0, "skipped": 0, "replaced": 0}
                for item in normalized:
                    saved = self._save(connection, item, on_conflict)
                    counts[{"created": "saved", "skipped": "skipped", "updated": "replaced"}[saved["status"]]] += 1
                result.update(counts)
            return result


def _column(value):
    if not value:
        return None
    text = str(value).strip().upper()
    if not re.fullmatch(r"[A-Z]{1,3}", text):
        raise ValueError("字段映射应使用 Excel 列字母，例如 A 或 AB。")
    index = column_index_from_string(text)
    if index > 16384:
        raise ValueError("字段映射超出 Excel 列范围。")
    return index


def inspect_daily(path, filename):
    from .analyzer import inspect_workbook
    from .daily_import import detect_paired_mapping
    information = inspect_workbook(path)
    aliases = {"date": ["日期", "业务日期", "销售日期", "date"], "product": ["商品编码", "商品id", "sku", "产品代码", "简称", "商品", "产品"],
               "name": ["商品名称", "产品名称", "产品全称", "name"], "shop": ["店铺", "仓库", "仓位", "shop"],
               "price": ["当日售价", "当前售价", "最新售价", "售价", "销售价", "零售价", "供价", "price"], "salesQuantity": ["当日销量", "销售数量", "日销量", "销量", "quantity"], "specification": ["商品规格", "销售规格", "规格"], "sourceSku": ["SKU", "sku", "SKU编码", "sku编码"], "warehouseId": ["仓库ID", "仓库id"], "sourceSystem": ["来源系统"]}
    best = None
    for sheet in information["sheets"]:
        columns = {}
        for field, names in aliases.items():
            for candidate in names:
                match = next((header for header in sheet.get("headers", []) if _text(header.get("label")).lower().replace(" ", "") == candidate), None)
                if match:
                    columns[field] = match["column"]
                    break
        score = (5 if columns.get("price") else 0) + (3 if columns.get("product") else 0) + (2 if columns.get("date") else 0) + (1 if "价" in sheet["name"] else 0)
        mapping = {"sheet": sheet["name"], "layout": "long", "headerRow": sheet.get("headerRow", 1), "columns": columns}
        paired = detect_paired_mapping(path, sheet["name"])
        if paired:
            mapping = paired
            score += 100
        sheet["mapping"] = mapping
        if best is None or score > best[0]:
            best = (score, mapping)
    guessed_date = ""
    match = re.search(r"(20\d{2})[-_.年]?(\d{2})[-_.月]?(\d{2})", filename)
    if match:
        try:
            guessed_date = date(*map(int, match.groups())).isoformat()
        except ValueError:
            pass
    return {"filename": filename, "sheets": information["sheets"], "mapping": best[1] if best else {"sheet": "", "headerRow": 1, "columns": {}},
            "suggestedDate": guessed_date, "notes": ["无日期列时请明确填写文件对应的业务日期。", "售价和销量必须属于同一天；最新售价不会自动填到历史日期。", "销量列应是当日销量，而非累计销量。"]}


def parse_import(entry, body):
    mapping = body.get("mapping")
    if not isinstance(mapping, dict):
        raise ValueError("请先配置价格表的工作表和字段。")
    if mapping.get("layout") == "paired_dates":
        from .daily_import import parse_paired
        return parse_paired(entry, body)
    wb = openpyxl.load_workbook(entry["path"], data_only=True)
    sheet_name = str(mapping.get("sheet", ""))
    if sheet_name not in wb.sheetnames:
        raise ValueError("所选工作表不存在，请重新选择。")
    sheet = wb[sheet_name]
    header = int(mapping.get("headerRow", 1))
    if header < 1 or header > 1000:
        raise ValueError("表头行应为 1—1000 的整数。")
    columns = {field: _column((mapping.get("columns") or {}).get(field)) for field in FIELDS}
    if not columns["product"]:
        raise ValueError("请指定商品标识列。")
    if not columns["price"] and not columns["salesQuantity"]:
        raise ValueError("请至少指定售价或当日销量中的一列。")
    snapshot_date = _date(body.get("snapshotDate")) if not columns["date"] else None
    # Only populated rows count; formatting to Excel's final row is not data.
    row_numbers = sorted({cell.row for cell in sheet._cells.values() if cell.value is not None and cell.row > header})
    if len(row_numbers) > 20000:
        raise ValueError("日记录单次最多导入 20,000 行，请分批处理。")
    records, issues = [], []
    seen, duplicates = set(), 0
    for row in row_numbers:
        get = lambda field: sheet.cell(row, columns[field]).value if columns[field] else None
        product = _text(get("product"))
        if not product or product in {"合计", "总计", "小计", "汇总", "total", "TOTAL"}:
            continue
        try:
            business_date = _date(get("date"), wb.epoch) if columns["date"] else snapshot_date
            normalized = _normalized({field: get(field) for field in FIELDS} | {"date": business_date,
                "source": {"filename": entry.get("filename") or Path(entry["path"]).name,
                           "sha256": entry.get("sha256", ""), "sheet": sheet_name, "row": row,
                           "columns": mapping.get("columns", {}), "snapshotDate": snapshot_date}})
            if columns["salesQuantity"]:
                title = _text(sheet.cell(header, columns["salesQuantity"]).value)
                if any(word in title for word in ("累计", "合计", "月销", "总销量")) and not body.get("quantityIsDaily"):
                    raise ValueError("所选销量列疑似累计／合计，不能直接记为当日销量；请确认口径或调整映射。")
                try:
                    title_date = _date(sheet.cell(header, columns["salesQuantity"]).value, wb.epoch)
                except ValueError:
                    title_date = None
                if title_date and title_date != business_date:
                    raise ValueError(f"销量列日期为 {title_date}，与业务日期 {business_date} 不一致。")
            key = normalized["identityKey"]
            if key in seen:
                duplicates += 1
                raise ValueError("同一文件中同日同商品同店铺重复；只保留首次记录，请核对。")
            seen.add(key)
            records.append(normalized)
        except ValueError as exc:
            issues.append({"row": row, "field": "", "message": str(exc), "product": product})
    wb.close()
    return {"records": records, "issues": issues, "validCount": len(records), "invalidCount": len(issues), "duplicateCount": duplicates}


def handle_daily_request(handler, method, path, query, body, data_dir, lookup):
    """Return False for another module's URL; errors are handled by the host."""
    if path != "/api/daily" and not path.startswith("/api/daily/"):
        return False
    body = body or {}
    store = DailyStore(data_dir)
    one = lambda key, default="": (query.get(key) or [default])[0]
    if method == "GET" and path == "/api/daily":
        handler.send_json(store.list(one("start"), one("end"), one("product"), one("shop"), one("deleted", "0") == "1", one("offset", "0"), one("limit", "500")))
    elif method == "GET" and path == "/api/daily/export":
        import base64
        from .daily_export import export_daily
        output = export_daily(store, query, one("format", "xlsx"))
        if isinstance(output, dict):
            handler.send_json(output)
        else:
            handler.send_json({"filename": "零售通-日记录." + one("format", "xlsx"),
                "contentType": "text/csv;charset=utf-8" if one("format") == "csv" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "dataBase64": base64.b64encode(output).decode("ascii")})
    elif method == "GET" and path == "/api/daily/catalog":
        from .daily_import import list_catalog
        handler.send_json(list_catalog(store))
    elif method == "POST" and path == "/api/daily/catalog":
        from .daily_import import save_catalog
        handler.send_json(save_catalog(store, body.get("record"), body.get("expectedRevision")))
    elif method == "POST" and path == "/api/daily/catalog/import":
        from .daily_import import import_catalog
        handler.send_json(import_catalog(store, lookup(body.get("workbookId")), body))
    elif method == "GET" and path == "/api/daily/inspect":
        entry = lookup(one("workbookId"))
        handler.send_json(inspect_daily(entry["path"], entry.get("filename") or Path(entry["path"]).name))
    elif method == "POST" and path == "/api/daily":
        handler.send_json(store.save(body.get("record"), body.get("onConflict", "skip")))
    elif method == "POST" and path == "/api/daily/import":
        from .daily_import import apply_catalog
        entry = lookup(body.get("workbookId"))
        result = apply_catalog(store, parse_import(entry, body))
        existing = store.existing_identity_keys()
        result["existingCount"] = sum(daily_identity(row) in existing for row in result["records"])
        if not body.get("previewOnly", True) and not result["records"]:
            raise ValueError("没有可保存的日记录，请先修正映射或数据问题。")
        result.update(store.reviewed_import(result["records"], body.get("onConflict", "skip"),
            body.get("previewOnly", True), body.get("previewFingerprint"),
            require_fingerprint=(body.get("mapping") or {}).get("layout") == "paired_dates",
            context={key: body.get(key) for key in ("mapping", "quantityIsDaily", "snapshotDate")}))
        handler.send_json(result)
    else:
        match = re.fullmatch(r"/api/daily/([a-f0-9]{32})(/restore|/revisions)?", path)
        if not match:
            handler.send_json({"error": "日记录接口不存在。"}, 404)
        elif method == "DELETE" and match[2] is None:
            handler.send_json(store.set_deleted(match[1], True))
        elif method == "POST" and match[2] == "/restore":
            handler.send_json(store.set_deleted(match[1], False))
        elif method == "GET" and match[2] == "/revisions":
            handler.send_json(store.revisions(match[1]))
        elif method == "GET" and match[2] is None:
            handler.send_json({"record": store.get(match[1])})
        else:
            handler.send_json({"error": "日记录操作不支持。"}, 405)
    return True
