"""Reviewed daily imports and explicit product/location aliases.

The catalog lives in daily.sqlite3 so existing portable backups carry it too.
Only identifiers and locations select aliases; names never merge products.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

import openpyxl
from openpyxl.utils import get_column_letter

from .daily_records import FIELDS, LABELS, _column, _date, _normalized, _text


CATALOG_FIELDS = ("sourceProduct", "sourceShop", "product", "name", "specification", "shop")
CATALOG_LABELS = dict(zip(CATALOG_FIELDS, ("来源商品ID", "来源仓位", "标准商品标识", "商品名称", "规格", "标准仓位")))


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def _same(left, right):
    return left == right


def review_records(connection, records, on_conflict="skip", context=None):
    review, counts = [], dict.fromkeys(("new", "unchanged", "conflict", "fill", "deleted"), 0)
    for incoming in records:
        old = connection.execute("SELECT * FROM daily_records WHERE date=? AND product=? AND shop=?", [incoming[k] for k in ("date", "product", "shop")]).fetchone()
        existing = dict(old) if old is not None else None
        if existing:
            existing["source"] = json.loads(existing["source"])
            existing["deleted"] = bool(existing["deleted"])
        differences = [{"field": field, "label": LABELS[field], "old": existing[field], "new": incoming[field]}
                       for field in ("name", "price", "salesQuantity") if existing and not _same(existing[field], incoming[field])]
        if existing is None:
            status = "new"
        elif existing["deleted"]:
            status = "deleted"
        elif not differences:
            status = "unchanged"
        elif all(item["old"] is None and item["new"] is not None for item in differences):
            status = "fill"
        else:
            status = "conflict"
        counts[status] += 1
        review.append({"status": status, "incoming": incoming, "existing": existing, "differences": differences})
    return {"review": review, "reviewCounts": counts,
            "previewFingerprint": fingerprint({"review": review, "onConflict": on_conflict, "context": context})}


def _merged_value(sheet, row, column):
    cell = sheet.cell(row, column)
    if cell.value is not None:
        return cell.value
    for merged in sheet.merged_cells.ranges:
        if merged.min_row <= row <= merged.max_row and merged.min_col <= column <= merged.max_col:
            return sheet.cell(merged.min_row, merged.min_col).value
    return None


def detect_paired_mapping(path, sheet_name=None):
    """Find explicit full-date headers over adjacent sales/price columns."""
    workbook = openpyxl.load_workbook(path, data_only=True)
    try:
        for sheet in workbook:
            if sheet_name is not None and sheet.title != sheet_name:
                continue
            for header in range(1, min(sheet.max_row, 12)):
                pairs = []
                for column in range(1, min(sheet.max_column, 512)):
                    left = _text(sheet.cell(header + 1, column).value).replace(" ", "")
                    right = _text(sheet.cell(header + 1, column + 1).value).replace(" ", "")
                    if {left, right} not in ({"销量", "售价"}, {"当日销量", "当日售价"}):
                        continue
                    value = _merged_value(sheet, header, column)
                    if value != _merged_value(sheet, header, column + 1):
                        continue
                    try:
                        day = _date(value, workbook.epoch)
                    except ValueError:
                        # Do not infer a year from a filename/current date.
                        day = ""
                    pairs.append({"date": day, "dateValue": _text(value), "dateColumn": get_column_letter(column),
                                  "salesQuantity": get_column_letter(column if "销量" in left else column + 1),
                                  "price": get_column_letter(column if "售价" in left else column + 1)})
                if not pairs:
                    continue
                aliases = {"product": ("商品id", "商品编码", "sku", "产品代码"),
                           "name": ("产品", "商品名称", "产品名称", "商品名"),
                           "shop": ("仓位", "仓库", "仓库名称", "店铺", "地区")}
                columns = {}
                for column in range(1, min(sheet.max_column, 512) + 1):
                    title = _text(_merged_value(sheet, header, column)).lower().replace(" ", "")
                    for field, names in aliases.items():
                        if title in names and field not in columns:
                            columns[field] = get_column_letter(column)
                if columns.get("product"):
                    return {"sheet": sheet.title, "layout": "paired_dates", "headerRow": header,
                            "dateHeaderRow": header, "valueHeaderRow": header + 1,
                            "dataStartRow": header + 2, "columns": columns, "datePairs": pairs}
        return None
    finally:
        workbook.close()


def parse_paired(entry, body):
    mapping = body["mapping"]
    workbook = openpyxl.load_workbook(entry["path"], data_only=True)
    try:
        if mapping.get("sheet") not in workbook.sheetnames:
            raise ValueError("所选工作表不存在，请重新选择。")
        sheet = workbook[mapping["sheet"]]
        columns = {field: _column((mapping.get("columns") or {}).get(field)) for field in ("product", "name", "shop")}
        if not columns["product"]:
            raise ValueError("请指定商品标识列。")
        header = int(mapping.get("dateHeaderRow", mapping.get("headerRow", 1)))
        value_header = int(mapping.get("valueHeaderRow", header + 1))
        start = int(mapping.get("dataStartRow", value_header + 1))
        if not 1 <= header < value_header < start <= 1002 or value_header > 1000:
            raise ValueError("请按顺序设置日期表头、销量／售价表头与数据起始行。")
        pairs = mapping.get("datePairs")
        if not isinstance(pairs, list) or not pairs or len(pairs) > 366:
            raise ValueError("请提供日期对应的售价、销量列。")
        resolved = []
        for pair in pairs:
            sales, price = _column(pair.get("salesQuantity")), _column(pair.get("price"))
            date_column = _column(pair.get("dateColumn")) or sales or price
            if not sales and not price:
                raise ValueError("日期组至少指定售价或销量列。")
            if sales and sales == price:
                raise ValueError("销量和售价不能使用同一列。")
            for column, field, aliases in ((sales, "销量", {"销量", "当日销量", "日销量", "销售数量"}),
                                           (price, "售价", {"售价", "当日售价", "销售价", "零售价"})):
                if column and _text(_merged_value(sheet, value_header, column)).replace(" ", "") not in aliases:
                    raise ValueError(f"{get_column_letter(column)}{value_header}不是{field}表头，请核对销量／售价表头行和列。")
            # Read source date afresh; the preview cannot silently rewrite its year.
            raw_date = _merged_value(sheet, header, date_column)
            try:
                actual = _date(raw_date, workbook.epoch)
                configured = _date(pair["date"]) if pair.get("date") else actual
                if configured != actual:
                    raise ValueError("日期组与原表完整日期不一致；请保持原始年月日。")
                for column in (sales, price):
                    if column and _date(_merged_value(sheet, header, column), workbook.epoch) != actual:
                        raise ValueError("售价／销量列不属于所选日期组，请核对日期来源和列。")
                day, error = actual, ""
            except ValueError as exc:
                day, error = "", str(exc)
            resolved.append((day, sales, price, error, date_column))
        row_numbers = sorted({cell.row for cell in sheet._cells.values() if cell.value is not None and cell.row >= start})
        if len(row_numbers) * len(resolved) > 20000:
            raise ValueError("日记录单次最多导入 20,000 条，请分批处理。")
        records, issues, seen = [], [], set()
        duplicates, empty = 0, 0
        for row in row_numbers:
            product = _text(_merged_value(sheet, row, columns["product"]))
            if not product or product.lower() in {"合计", "总计", "小计", "汇总", "total"}:
                continue
            name = _text(_merged_value(sheet, row, columns["name"])) if columns["name"] else product
            shop = _text(_merged_value(sheet, row, columns["shop"])) if columns["shop"] else ""
            for day, sales, price, error, date_column in resolved:
                quantity = sheet.cell(row, sales).value if sales else None
                amount = sheet.cell(row, price).value if price else None
                if quantity in (None, "") and amount in (None, ""):
                    empty += 1
                    continue
                try:
                    if error:
                        raise ValueError(error)
                    record = _normalized({"date": day, "product": product, "name": name, "shop": shop,
                        "price": amount, "salesQuantity": quantity, "source": {
                            "filename": entry.get("filename") or Path(entry["path"]).name,
                            "sha256": entry.get("sha256", ""), "sheet": sheet.title, "row": row,
                            "layout": "paired_dates", "dateCell": f"{get_column_letter(date_column)}{header}",
                            "priceCell": f"{get_column_letter(price)}{row}" if price else "",
                            "salesCell": f"{get_column_letter(sales)}{row}" if sales else ""}})
                    key = (day, product, shop)
                    if key in seen:
                        duplicates += 1
                        raise ValueError("同一文件中同日同商品同仓位重复；只保留首次记录，请核对。")
                    seen.add(key)
                    records.append(record)
                except ValueError as exc:
                    issues.append({"row": row, "date": day, "product": product, "field": "", "message": str(exc)})
        return {"records": records, "issues": issues, "validCount": len(records), "invalidCount": len(issues),
                "duplicateCount": duplicates, "emptyCount": empty, "layout": "paired_dates"}
    finally:
        workbook.close()


def catalog_row(row):
    if row is None:
        return None
    result = dict(row)
    result["source"] = json.loads(result["source"])
    return result


def normalize_catalog(record):
    if not isinstance(record, dict):
        raise ValueError("商品对照应为对象。")
    normalized = {key: _text(record.get(key)) for key in CATALOG_FIELDS}
    if not normalized["sourceProduct"] or not normalized["product"]:
        raise ValueError("来源商品ID与标准商品标识均不能为空。")
    if any(len(value) > (500 if key == "name" else 200) for key, value in normalized.items()):
        raise ValueError("商品对照字段过长。")
    normalized["name"] = normalized["name"] or normalized["product"]
    normalized["source"] = record.get("source") or "人工确认"
    json.dumps(normalized["source"], ensure_ascii=False, allow_nan=False)
    return normalized


def list_catalog(store):
    with store._connect() as connection:
        rows = connection.execute("SELECT * FROM product_catalog ORDER BY sourceProduct,sourceShop").fetchall()
    return {"items": [catalog_row(row) for row in rows], "total": len(rows)}


def _save_catalog(connection, record, on_conflict="skip", expected_revision=None):
    old = connection.execute("SELECT * FROM product_catalog WHERE sourceProduct=? AND sourceShop=?", [record["sourceProduct"], record["sourceShop"]]).fetchone()
    if expected_revision is not None and (old is None or old["revision"] != int(expected_revision)):
        raise ValueError("商品对照已变化，请刷新后重新确认。")
    if old and (on_conflict == "skip" or all(record[key] == old[key] for key in CATALOG_FIELDS)):
        return {"status": "skipped", "record": catalog_row(old)}
    now = datetime.now(timezone.utc).isoformat()
    if old:
        connection.execute("UPDATE product_catalog SET product=?,name=?,specification=?,shop=?,source=?,revision=revision+1,updatedAt=? WHERE sourceProduct=? AND sourceShop=?",
            [record[key] for key in ("product", "name", "specification", "shop")] + [json.dumps(record["source"], ensure_ascii=False), now, record["sourceProduct"], record["sourceShop"]])
    else:
        connection.execute("INSERT INTO product_catalog(sourceProduct,sourceShop,product,name,specification,shop,source,revision,updatedAt) VALUES(?,?,?,?,?,?,?,1,?)",
            [record[key] for key in CATALOG_FIELDS] + [json.dumps(record["source"], ensure_ascii=False), now])
    row = connection.execute("SELECT * FROM product_catalog WHERE sourceProduct=? AND sourceShop=?", [record["sourceProduct"], record["sourceShop"]]).fetchone()
    return {"status": "updated" if old else "created", "record": catalog_row(row)}


def save_catalog(store, record, expected_revision=None):
    record = normalize_catalog(record)
    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        return _save_catalog(connection, record, "replace", expected_revision)


def apply_catalog(store, result):
    links = {(item["sourceProduct"], item["sourceShop"]): item for item in list_catalog(store)["items"]}
    records, seen = [], set()
    for incoming in result["records"]:
        item = dict(incoming)
        link = links.get((item["product"], item["shop"]))
        if link:
            item["source"] = {"original": item["source"], "catalog": {key: link[key] for key in (*CATALOG_FIELDS, "revision")}}
            item.update({key: link[key] for key in ("product", "name", "shop")})
        key = tuple(item[key] for key in ("date", "product", "shop"))
        if key in seen:
            result["issues"].append({"row": incoming.get("source", {}).get("row", ""), "product": incoming["product"], "message": "商品对照后出现同日同商品同仓位重复；未累计，请核对对照。"})
            result["duplicateCount"] += 1
            continue
        seen.add(key)
        records.append(item)
    result.update(records=records, validCount=len(records), invalidCount=len(result["issues"]))
    return result


def parse_catalog(entry, sheet_name=None):
    workbook = openpyxl.load_workbook(entry["path"], data_only=True)
    try:
        for sheet in workbook:
            if sheet_name and sheet.title != sheet_name:
                continue
            for header in range(1, min(12, sheet.max_row) + 1):
                titles = {_text(cell.value).replace(" ", "").lower(): cell.column for cell in sheet[header] if cell.value is not None}
                source_column = next((titles[key] for key in ("商品id", "商品编码", "sku", "产品代码") if key in titles), None)
                if not source_column:
                    continue
                pick = lambda keys: next((titles[key] for key in keys if key in titles), None)
                columns = {"sourceProduct": source_column, "name": pick(("商品名称", "产品名称", "产品", "商品名")),
                           "specification": pick(("销售规格", "规格", "商品规格")), "sourceShop": pick(("仓库名称", "仓位", "仓库", "店铺", "地区"))}
                if not columns["name"] and not columns["specification"]:
                    continue
                populated = sorted({cell.row for cell in sheet._cells.values() if cell.value is not None and cell.row > header})
                if len(populated) > 20000:
                    raise ValueError("商品对照单次最多导入20,000行。")
                records, issues, seen = [], [], {}
                for row in populated:
                    values = {key: _text(_merged_value(sheet, row, column)) if column else "" for key, column in columns.items()}
                    if not values["sourceProduct"] or values["sourceProduct"] in {"合计", "总计", "小计"}:
                        continue
                    record = normalize_catalog(values | {"product": values["sourceProduct"], "shop": values["sourceShop"],
                        "source": {"filename": entry.get("filename") or Path(entry["path"]).name, "sheet": sheet.title, "row": row, "sha256": entry.get("sha256", "")}})
                    key = (record["sourceProduct"], record["sourceShop"])
                    if key in seen:
                        if any(record[field] != seen[key][field] for field in ("name", "specification")):
                            issues.append({"row": row, "product": record["sourceProduct"], "message": "相同商品ID和仓位出现不同名称或规格；该标识全部暂不导入，请拆分标准对照后处理。"})
                            records = [item for item in records if (item["sourceProduct"], item["sourceShop"]) != key]
                        continue
                    seen[key] = record
                    records.append(record)
                return {"records": records, "issues": issues, "validCount": len(records), "invalidCount": len(issues)}
        raise ValueError("没有识别到含商品ID及名称／规格的商品资料表。")
    finally:
        workbook.close()


def review_catalog(connection, records, on_conflict="skip"):
    review, counts = [], dict.fromkeys(("new", "unchanged", "conflict"), 0)
    for incoming in records:
        old = catalog_row(connection.execute("SELECT * FROM product_catalog WHERE sourceProduct=? AND sourceShop=?", [incoming["sourceProduct"], incoming["sourceShop"]]).fetchone())
        differences = [{"field": field, "label": CATALOG_LABELS[field], "old": old[field], "new": incoming[field]}
                       for field in ("product", "name", "specification", "shop") if old and old[field] != incoming[field]]
        status = "new" if not old else "conflict" if differences else "unchanged"
        counts[status] += 1
        review.append({"status": status, "incoming": incoming, "existing": old, "differences": differences})
    return {"review": review, "reviewCounts": counts,
            "previewFingerprint": fingerprint({"review": review, "onConflict": on_conflict})}


def import_catalog(store, entry, body):
    result = parse_catalog(entry, body.get("sheet"))
    conflict = body.get("onConflict", "skip")
    if conflict not in {"skip", "replace"}:
        raise ValueError("商品对照仅支持保留或显式更新。")
    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        result.update(review_catalog(connection, result["records"], conflict))
        if body.get("previewOnly", True):
            return result
        if body.get("previewFingerprint") != result["previewFingerprint"]:
            raise ValueError("请先预览商品对照；内容或已有对照变化后需重新预览。")
        result.update(saved=0, replaced=0, skipped=0)
        for item in result["records"]:
            saved = _save_catalog(connection, item, conflict)
            result[{"created": "saved", "updated": "replaced", "skipped": "skipped"}[saved["status"]]] += 1
        return result
