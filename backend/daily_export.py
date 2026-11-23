"""Export a complete filtered daily ledger without changing sources or stores."""
from __future__ import annotations

import base64
import csv
from datetime import date
from io import BytesIO, StringIO
import json
import math

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

MAX_RECORDS = 50000
HEADERS = ("业务日期", "商品标识", "商品名称", "店铺／仓库", "当日售价", "当日销量", "记录状态", "版本", "来源")


def _literal(value):
    """Spreadsheet text remains text, including formula-like identifiers."""
    if value is None:
        return ""
    text = str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) or text[:1] in ("\t", "\r", "\n") else text


def _source(value):
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _style(sheet, widths):
    sheet.freeze_panes = "A2"
    sheet.sheet_view.showGridLines = False
    sheet.auto_filter.ref = sheet.dimensions
    for row in sheet:
        for cell in row:
            cell.font = Font(name="Arial", size=11)
            cell.alignment = Alignment(vertical="center", wrap_text=True)
    for cell in sheet[1]:
        cell.font = Font(name="Arial", size=11, color="FFFFFF", bold=True)
        cell.fill = PatternFill("solid", fgColor="1F4E79")
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(index)].width = width


def export_daily(store, query, format="xlsx"):
    if format not in {"xlsx", "csv"}:
        raise ValueError("日记录导出仅支持 xlsx 或 csv。")
    def get(key, default=""):
        value = query.get(key, [default])
        return value[0] if isinstance(value, list) and value else value
    deleted = get("deleted", "0") == "1"
    filters = {key: str(get(key) or "") for key in ("start", "end", "product", "shop")}
    records = []
    while True:
        page = store.list(**filters, deleted=deleted, offset=len(records), limit=1000)
        if page["total"] > MAX_RECORDS:
            raise ValueError(f"当前范围超过{MAX_RECORDS:,}条，请缩小日期或商品范围后导出。")
        records.extend(page["items"])
        if not page["hasMore"]:
            break
    scope = [("开始日期", filters["start"] or "不限"), ("结束日期", filters["end"] or "不限"),
             ("商品筛选", filters["product"] or "全部"), ("店铺／仓库筛选", filters["shop"] or "全部"),
             ("记录范围", "已删除" if deleted else "当前有效"), ("记录数量", len(records)),
             ("销量口径", "记录来源的当日销量，未自动扣除售后"),
             ("售价口径", "当日记录价，不代表实际成交均价"),
             ("商品关联范围", "当前全量关联仅供参考，历史日记录使用保存时的商品和数值")]
    rows = [[item["date"], item["product"], item["name"], item["shop"], item["price"],
             item["salesQuantity"], "已删除" if item["deleted"] else "有效", item["revision"],
             _source(item["source"])] for item in records]
    if format == "csv":
        stream = StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(["零售通每日价格与销量"])
        writer.writerows([[key, _literal(value) if isinstance(value, str) else value] for key, value in scope])
        writer.writerow([])
        writer.writerow(HEADERS)
        writer.writerows([[_literal(v) if isinstance(v, str) else v for v in row] for row in rows])
        payload = b"\xef\xbb\xbf" + stream.getvalue().encode("utf-8")
        content_type = "text/csv;charset=utf-8"
    else:
        book = openpyxl.Workbook()
        ledger = book.active
        ledger.title = "每日记录"
        ledger.append(HEADERS)
        for row in rows:
            row = list(row)
            row[0] = date.fromisoformat(row[0])
            ledger.append(row)
        for row in ledger.iter_rows(min_row=2):
            row[0].number_format = "yyyy-mm-dd"
            for index in (1, 2, 3, 8):
                row[index].data_type = "s"
            for index in (4, 5):
                row[index].number_format = "#,##0.00"
        _style(ledger, (14, 25, 28, 22, 16, 16, 12, 10, 64))
        details = book.create_sheet("导出范围")
        details.append(["项目", "范围与口径"])
        for key, value in scope:
            details.append([key, value])
            if isinstance(value, str):
                details.cell(details.max_row, 2).data_type = "s"
        _style(details, (24, 68))
        # Catalog is context only: old ledger rows retain their saved identifiers.
        catalog = book.create_sheet("商品关联")
        catalog.append(["来源商品ID", "来源仓位", "标准商品标识", "商品名称", "规格", "标准仓位", "版本"])
        with store._connect() as connection:
            exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='product_catalog'").fetchone()
            entries = connection.execute("SELECT sourceProduct,sourceShop,product,name,specification,shop,revision FROM product_catalog ORDER BY sourceProduct,sourceShop").fetchall() if exists else []
        for entry in entries:
            catalog.append(list(entry))
            for cell in list(catalog[catalog.max_row])[:6]:
                cell.data_type = "s"
        _style(catalog, (25, 22, 25, 28, 24, 22, 10))
        stream = BytesIO()
        book.save(stream)
        book.close()
        payload = stream.getvalue()
        # Reopen the actual export: preserve dates, identifiers, missing and zero.
        reopened = openpyxl.load_workbook(BytesIO(payload), data_only=False)
        try:
            if reopened["每日记录"].max_row != len(rows) + 1:
                raise ValueError("导出重读校验失败。")
            for index, expected in enumerate(rows, 2):
                actual = [c.value for c in reopened["每日记录"][index]]
                if actual[0].date().isoformat() != expected[0]:
                    raise ValueError("导出日期重读校验失败。")
                for column in range(1, len(expected)):
                    value = None if expected[column] == "" else expected[column]
                    equal = math.isclose(actual[column], value, rel_tol=1e-14, abs_tol=1e-12) if isinstance(value, (int, float)) and isinstance(actual[column], (int, float)) else actual[column] == value
                    if not equal:
                        raise ValueError("导出内容重读校验失败。")
        finally:
            reopened.close()
        content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return {"filename": f"零售通-每日记录.{format}", "contentType": content_type,
            "dataBase64": base64.b64encode(payload).decode("ascii"), "count": len(records), "scope": dict(scope)}
