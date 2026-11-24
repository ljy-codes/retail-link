"""Read-only, configurable Excel analysis for the operations cockpit.

All business outputs are rebuilt from dated detail. Stored Excel values are only
used as explicit source amounts where their complete scope matches the report.
No workbook is executed or modified.
"""
from __future__ import annotations

import calendar
import copy
import math
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

import openpyxl
from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.utils.datetime import from_excel

from .product_identity import resolve_identity, digest


ROLE_NAMES = {
    "summary": "月计划", "products": "商品规格", "conversion": "商品对照",
    "inventory": "现货库存", "sales": "平台销量", "outbound": "仓库出库",
    "orders": "报单记录", "transit": "在途记录", "loss": "货损记录",
}
ROLE_ALIASES = {
    "summary": ["汇总", "月计划", "计划表", "经营汇总", "计划"],
    "products": ["规格价格明细", "商品主档", "商品规格", "商品资料", "产品资料", "商品", "规格"],
    "conversion": ["转换表", "商品对照", "编码对照", "对应表"],
    "inventory": ["库存表", "现货库存", "仓库库存", "库存", "盘点"],
    "sales": ["销售明细", "销量明细", "平台销量", "销售数据", "销量", "销售"],
    "outbound": ["出库明细", "出库记录", "出库"],
    "orders": ["订单明细", "报单记录", "报单", "订货", "采购订单", "订单"],
    "transit": ["在途库存表", "在途库存", "在途记录", "在途"],
    "loss": ["货损", "货损明细", "损耗", "损耗明细"],
}
FIELD_ALIASES = {
    "product": ["产品简称", "简称", "产品", "商品", "sku", "标准商品", "商品编码", "产品名称", "商品名称"],
    "code": ["产品代码", "商品代码", "代码", "商品编码", "产品编码", "商品id", "sku编码"],
    "name": ["产品全称", "产品名称", "商品名称", "名称"],
    "warehouse": ["仓位", "仓库", "仓库名称", "店铺", "渠道"],
    "warehouseId": ["仓库id", "仓位id", "仓库编号"],
    "sourceSku": ["来源sku", "sku编码", "sku", "规格id"],
    "sourceSystem": ["来源系统", "来源平台"],
    "specification": ["商品规格", "销售规格", "规格"],
    "pack": ["箱规", "转化率", "转换率", "每箱数量", "装箱数", "包装数量"],
    "plan": ["计划量", "月计划", "计划金额", "月计划金额", "目标", "计划数量", "计划"],
    "stockLarge": ["库存数量（大单位）", "大单位库存", "箱数", "库存箱数"],
    "stockSmall": ["库存数量（小单位）", "小单位库存", "可用库存", "库存数量", "库存", "现货库存"],
    "basePriceLarge": ["到岸价（大单位）", "到案件", "大单位单价", "每箱价格", "箱价"],
    "basePrice": ["到岸价（小单位）", "到岸价", "单价", "基准价", "参考价", "成本价", "小单位单价"],
    "supplyPrice": ["供价", "出库供价", "销售单价", "供货价", "实际单价", "成交价"],
    "date": ["日期", "销售日期", "出库日期", "下单日期", "订单日期", "业务日期", "统计日期", "时间"],
    "quantity": ["数量", "销量", "销售数量", "报单数量", "订货数量", "出库数量", "货损数量", "在途数量", "合计"],
    "amount": ["金额", "到岸金额", "订单金额", "货损金额", "库存金额", "出库金额"],
    "supplyAmount": ["销售金额", "出库销售金额（数值）", "出库销售金额", "供价金额", "实销金额"],
}
FIELD_LABELS = {
    "product": "商品简称 / 标识", "code": "来源商品编码", "name": "商品名称", "pack": "箱规 / 转换率",
    "warehouseId": "仓库ID", "sourceSku": "来源SKU", "sourceSystem": "来源系统", "specification": "规格",
    "warehouse": "仓库 / 渠道", "plan": "月计划", "stockLarge": "库存（大单位）", "stockSmall": "库存（小单位）",
    "basePriceLarge": "基准价（大单位）", "basePrice": "基准单价", "supplyPrice": "供货单价",
    "date": "日期（长表）", "quantity": "数量（长表 / 合计）", "amount": "源金额",
    "supplyAmount": "源供价金额", "amountProduct": "辅助汇总区商品标识",
}
IDENTITY_FIELDS = ["warehouseId", "sourceSku", "sourceSystem", "specification"]
COMMON = ["product", "code", "warehouse", *IDENTITY_FIELDS, "date", "quantity", "basePrice", "supplyPrice", "amount", "supplyAmount", "amountProduct"]
ROLE_FIELDS = {
    "summary": ["product", "name", "plan", *IDENTITY_FIELDS],
    "products": ["product", "code", "name", "pack", "basePriceLarge", "basePrice", *IDENTITY_FIELDS],
    "conversion": ["product", "code", "name", "pack", "basePriceLarge", "basePrice", *IDENTITY_FIELDS],
    "inventory": ["product", "code", "name", "warehouse", "pack", "stockLarge", "stockSmall", "basePriceLarge", "basePrice", "amount", "date", *IDENTITY_FIELDS],
    **{role: COMMON[:] for role in ["sales", "outbound", "orders", "transit", "loss"]},
}
DEFAULTS = {
    "targetRate": .85, "averageDays": 7, "transitDays": 5,
    "planBasis": "orders_amount", "amountMode": "source",
    "alignSalesYear": False, "inventoryComparable": False, "overrides": {},
}


def _text(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _norm(value):
    return re.sub(r"[\s（）()_\-]", "", _text(value)).lower()


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (float, int)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            result = float(value.replace(",", "").replace("¥", "").replace("￥", "").strip())
            return result if math.isfinite(result) else None
        except ValueError:
            return None
    return None


def _date(value, epoch=None, year=None):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and 20000 <= value <= 80000:
        try:
            return from_excel(value, epoch=epoch or openpyxl.utils.datetime.CALENDAR_WINDOWS_1900).date()
        except (ValueError, OverflowError, AttributeError):
            return None
    text = _text(value)
    text = text.replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-").replace(".", "-")
    try:
        if re.match(r"^\d{4}-\d{1,2}-\d{1,2}", text):
            parts = text.split(" ")[0].split("T")[0].split("-")
            return date(int(parts[0]), int(parts[1]), int(parts[2]))
        if year and re.fullmatch(r"\d{1,2}-\d{1,2}", text):
            month, day = map(int, text.split("-"))
            return date(year, month, day)
    except ValueError:
        return None
    return None


def _serial(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


@lru_cache(maxsize=6)
def _cached_load(path, mtime_ns, size):
    # Sparse bounds matter: supplied sheets contain formatting at row 1,048,565.
    formulas = openpyxl.load_workbook(path, data_only=False, read_only=False, keep_links=False)
    values = openpyxl.load_workbook(path, data_only=True, read_only=False, keep_links=False)
    return formulas, values


def _load(path):
    path = Path(path).resolve()
    if path.suffix.lower() not in (".xlsx", ".xlsm"):
        raise ValueError("请选择 .xlsx 或 .xlsm 工作簿；旧版 .xls 请先另存为 .xlsx。")
    stat = path.stat()
    return _cached_load(str(path), stat.st_mtime_ns, stat.st_size)


def _nonempty(sheet):
    return [cell for cell in sheet._cells.values() if cell.value is not None]


def _bounds(sheet):
    cells = _nonempty(sheet)
    return (max((c.row for c in cells), default=0), max((c.column for c in cells), default=0))


def _row_numbers(sheet, first):
    return sorted({c.row for c in _nonempty(sheet) if c.row >= first})


def _col(value):
    value = _text(value).upper().replace("$", "")
    if not value:
        return None
    if not re.fullmatch("[A-Z]{1,3}", value):
        raise ValueError(f"列配置 {value!r} 无效，请使用 A、B、AA 等列字母。")
    result = column_index_from_string(value)
    if result > 16384:
        raise ValueError("列配置超出 Excel 列数。")
    return result


def _header_row(sheet):
    aliases = {_norm(a) for values in FIELD_ALIASES.values() for a in values}
    scores = defaultdict(float)
    for c in _nonempty(sheet):
        if c.row <= 15 and isinstance(c.value, str) and not c.value.startswith("="):
            n = _norm(c.value)
            scores[c.row] += 3 if n in aliases else (.1 if n else 0)
    return max(scores, key=lambda r: (scores[r], -r)) if scores else 1


def _find_col(headers, field):
    for alias in FIELD_ALIASES.get(field, []):
        for c, h in headers:
            if _norm(h) == _norm(alias):
                return get_column_letter(c)
    return ""


def _sheet_match(names, role):
    for alias in ROLE_ALIASES[role]:
        for name in names:
            if _norm(name) == _norm(alias):
                return name
    for alias in ROLE_ALIASES[role]:
        if len(alias) > 1:
            for name in names:
                if _norm(alias) in _norm(name):
                    # Avoid identifying the transit sheet as on-hand inventory.
                    if role == "inventory" and "在途" in name:
                        continue
                    return name
    return ""


def _date_columns(sheet, header_row, epoch, year=None):
    result = []
    for c in _nonempty(sheet):
        if c.row == header_row:
            d = _date(c.value, epoch, year)
            if d:
                result.append((c.column, d))
    return sorted(result)


def _guess_mapping(sheet, role):
    hr = _header_row(sheet)
    headers = sorted((c.column, c.value) for c in _nonempty(sheet) if c.row == hr)
    columns = {field: _find_col(headers, field) for field in ROLE_FIELDS[role]}
    # Where a sheet contains a separately keyed right-hand summary block, fields
    # in that block must join by product, never by physical row number.
    products = [(c, h) for c, h in headers if _norm(h) in ("产品", "商品", "简称", "产品简称")]
    if role in ("outbound", "orders", "transit", "loss") and len(products) > 1:
        aux_start = products[-1][0]
        aux_headers = [(c, h) for c, h in headers if c >= aux_start]
        main_headers = [(c, h) for c, h in headers if c < aux_start]
        columns["amountProduct"] = get_column_letter(aux_start)
        for field in ("supplyPrice", "supplyAmount"):
            columns[field] = _find_col(aux_headers, field) or _find_col(main_headers, field)
        # The sample marks verified source values as 金额（数值）.
        source_amount = next((get_column_letter(c) for c, h in aux_headers if _norm(h) == "金额数值"), "")
        if source_amount:
            columns["amount"] = source_amount
        else:
            columns["amount"] = _find_col(aux_headers, "amount") or _find_col(main_headers, "amount")
        # Main-detail base price must not come from the separate summary block.
        columns["basePrice"] = _find_col(main_headers, "basePrice")
    if role == "inventory":
        # A sheet having explicit large stock and a single 到岸价 uses case price.
        if columns.get("stockLarge") and not columns.get("basePriceLarge") and columns.get("basePrice"):
            columns["basePriceLarge"], columns["basePrice"] = columns["basePrice"], ""
    return {"sheet": sheet.title, "headerRow": hr, "columns": columns}


def inspect_workbook(path):
    formulas, values = _load(path)
    mappings = {}
    sheets = []
    for sheet in formulas:
        rows, cols = _bounds(sheet)
        if rows > 100000 or cols > 512:
            raise ValueError(f"工作表“{sheet.title}”实际内容超过 100,000 行或 512 列，请先拆分数据。")
        hr = _header_row(sheet)
        headers = [{"column": get_column_letter(c), "label": _text(values[sheet.title].cell(hr, c).value)} for c in range(1, cols + 1)]
        preview = [[_serial(values[sheet.title].cell(r, c).value) for c in range(1, min(cols, 16) + 1)] for r in range(hr, min(rows, hr + 4) + 1)]
        sheets.append({"name": sheet.title, "rows": rows, "columns": cols, "headerRow": hr, "headers": headers, "preview": preview})
    for role in ROLE_NAMES:
        name = _sheet_match(values.sheetnames, role)
        mappings[role] = _guess_mapping(values[name], role) if name else {"sheet": "", "headerRow": 1, "columns": {f: "" for f in ROLE_FIELDS[role]}}
    observed_dates = []
    # Report date follows primary business dates, not a mismatched sales year or
    # future-planned transit row. Date headers alone do not prove observed data.
    for role in ("orders", "outbound", "sales"):
        mapping = mappings[role]
        if not mapping["sheet"]:
            continue
        sheet = values[mapping["sheet"]]
        dc = _date_columns(sheet, mapping["headerRow"], values.epoch)
        product_column = _col(mapping["columns"].get("product"))
        detail_rows = [r for r in _row_numbers(sheet, mapping["headerRow"] + 1)
                       if product_column and _text(sheet.cell(r, product_column).value)
                       and not _total_label(sheet.cell(r, product_column).value)]
        for c, d in dc:
            if any(_number(sheet.cell(r, c).value) is not None for r in detail_rows):
                observed_dates.append(d)
        date_col = _col(mapping["columns"].get("date"))
        if date_col:
            observed_dates.extend(d for r in detail_rows if (d := _date(sheet.cell(r, date_col).value, values.epoch)))
    asof = max(observed_dates) if observed_dates else date.today()
    config = {**copy.deepcopy(DEFAULTS), "asOf": asof.isoformat(), "period": asof.strftime("%Y-%m"), "mappings": mappings}
    return {"filename": Path(path).name, "sheets": sheets, "config": config,
            "mappingSchema": [{"role": role, "label": label, "fields": [{"key": field, "label": FIELD_LABELS[field]} for field in ROLE_FIELDS[role]]} for role, label in ROLE_NAMES.items()]}


def preview_workbook(path, sheet, offset=0, limit=30):
    formulas, values = _load(path)
    if sheet not in values.sheetnames:
        raise ValueError("找不到所选工作表。")
    offset = max(0, int(offset))
    limit = max(1, min(100, int(limit)))
    height, width = _bounds(formulas[sheet])
    rows = []
    for r in range(offset + 1, min(height, offset + limit) + 1):
        cells = []
        for c in range(1, min(width, 100) + 1):
            raw = formulas[sheet].cell(r, c)
            item = {"column": get_column_letter(c), "value": _serial(values[sheet].cell(r, c).value)}
            if raw.data_type == "f":
                item["formula"] = raw.value
            cells.append(item)
        rows.append({"row": r, "cells": cells})
    hr = _header_row(values[sheet])
    return {"filename": Path(path).name, "sheet": sheet, "offset": offset,
            "columns": [{"column": get_column_letter(c), "label": _text(values[sheet].cell(hr, c).value)} for c in range(1, min(width, 100) + 1)],
            "rows": rows, "totalRows": height, "totalColumns": width}


class _Issues:
    def __init__(self):
        self.items = []
        self.seen = set()

    def add(self, code, title, detail, severity="warning", sheet="", cell="", product=""):
        key = (code, sheet, cell, product)
        if key in self.seen:
            return
        self.seen.add(key)
        self.items.append({"id": "issue-" + str(len(self.items) + 1), "severity": severity, "code": code,
                           "title": title, "detail": detail, "sheet": sheet, "cell": cell, "product": product})


def _sum(values):
    values = [x for x in values if x is not None]
    return sum(values) if values else None


def _ratio(a, b):
    return a / b if a is not None and b is not None and b != 0 else None


def _total_label(value):
    return _norm(value) in ("合计", "总计", "汇总", "小计", "总合计", "total", "grandtotal")


def _plan_assessment(plan, completed, target, remaining_days):
    """Assess one product before aggregating, without offsetting other products.

    A missing or nonpositive plan does not establish a valid target. Preserve
    that uncertainty separately from a known zero completion or a met target.
    """
    result = {"targetAmount": None, "targetGap": None, "dailyRequired": None,
              "remainingDays": remaining_days, "assessmentStatus": "unknown"}
    if plan is None:
        result.update(assessmentReasonCode="missing_plan", assessmentReason="未提供月计划")
        return result
    if plan <= 0:
        result.update(assessmentReasonCode="nonpositive_plan", assessmentReason="月计划需大于0，当前无法评价达标情况")
        return result
    result["targetAmount"] = plan * target
    if completed is None:
        result.update(assessmentReasonCode="missing_completed", assessmentReason="未提供当前考核口径的完成数据")
        return result
    # Only remove machine-precision noise at the boundary; do not round each
    # product's requirement to currency units or average its achievement rate.
    precision = max(1e-9, math.ulp(completed) * 2, math.ulp(result["targetAmount"]) * 2)
    met = completed >= result["targetAmount"] or math.isclose(
        completed, result["targetAmount"], rel_tol=0., abs_tol=precision)
    result["targetGap"] = 0. if met else result["targetAmount"] - completed
    result["assessmentStatus"] = "met" if met else "below"
    if met:
        result.update(dailyRequired=0., assessmentReasonCode="met", assessmentReason="已达到单品目标门槛")
    elif remaining_days:
        result.update(dailyRequired=result["targetGap"] / remaining_days,
                      assessmentReasonCode="below", assessmentReason="尚未达到单品目标门槛")
    else:
        result.update(assessmentReasonCode="period_ended", assessmentReason="统计月份已结束，仍有未完成缺口")
    return result


def analyze_workbook(path, config):
    inspected = inspect_workbook(path)
    options = {**copy.deepcopy(inspected["config"]), **copy.deepcopy(config or {})}
    options["mappings"] = {**inspected["config"]["mappings"], **options.get("mappings", {})}
    asof = _date(options.get("asOf"))
    if not asof:
        raise ValueError("统计截止日期无效。")
    try:
        year, month = map(int, options["period"].split("-"))
        month_start = date(year, month, 1)
        month_end = date(year, month, calendar.monthrange(year, month)[1])
        target = float(options.get("targetRate", .85))
        average_days = int(options.get("averageDays", 7))
        transit_days = int(options.get("transitDays", 5))
        if not 0 < target <= 2 or not 1 <= average_days <= 365 or not 1 <= transit_days <= 365:
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise ValueError("请检查统计月份、目标比例、销量天数与在途天数配置。")
    cutoff = min(asof, month_end)
    if asof < month_start:
        raise ValueError("统计截止日期不能早于统计月份。")
    basis = options.get("planBasis", "orders_amount")
    if basis not in ("orders_amount", "outbound_amount", "orders_quantity", "outbound_quantity", "sales_quantity"):
        raise ValueError("不支持的计划考核口径。")
    if options.get("amountMode") not in ("source", "calculated"):
        raise ValueError("金额口径只能选择 source 或 calculated。")
    formulas, wb = _load(path)
    issues = _Issues()
    catalog = options.get("productCatalog", [])
    if not isinstance(catalog, list) or len(catalog) > 20000:
        raise ValueError("商品关联配置应为列表，最多20,000条。")
    meta = {}
    identity_cache = {}
    catalog_index = defaultdict(list)
    for alias in catalog:
        if not isinstance(alias, dict):
            raise ValueError("商品关联配置含无效条目。")
        catalog_index[_text(alias.get("sourceProduct"))].append(alias)
        if _text(alias.get("product")) != _text(alias.get("sourceProduct")):
            catalog_index[_text(alias.get("product"))].append(alias)
    sources = []
    role_data = {}
    date_coverage = {}
    platform_codes = defaultdict(dict)

    def mapped(role):
        m = options["mappings"].get(role) or {}
        if not m.get("sheet"):
            return None
        if m["sheet"] not in wb.sheetnames:
            issues.add("MISSING_SHEET", "配置的工作表不存在", f"{ROLE_NAMES[role]}指定“{m['sheet']}”，当前文件中未找到。", "error", m["sheet"])
            return None
        try:
            hr = int(m.get("headerRow", 1))
            if hr < 1 or hr > 100:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError(f"{ROLE_NAMES[role]}的表头行必须是 1—100。")
        cols = {key: _col(value) for key, value in m.get("columns", {}).items()}
        if not cols.get("product"):
            issues.add("MISSING_PRODUCT_COLUMN", "缺少商品标识列", f"请在字段映射中设置{ROLE_NAMES[role]}的商品标识列。", "error", m["sheet"])
            return None
        return wb[m["sheet"]], hr, cols

    def get(sheet, row, cols, key):
        c = cols.get(key)
        return sheet.cell(row, c).value if c else None

    def ref(sheet, row, cols, key):
        c = cols.get(key)
        return f"{sheet.title}!{get_column_letter(c)}{row}" if c else f"{sheet.title}!第{row}行"

    def identify(sheet, row, cols):
        cache_key = (sheet.title, row, tuple(sorted(cols.items())))
        if cache_key not in identity_cache:
            raw = _text(get(sheet, row, cols, "product"))
            identity = resolve_identity(catalog_index.get(raw, []), raw, _text(get(sheet, row, cols, "warehouse")),
                _text(get(sheet, row, cols, "specification")), _text(get(sheet, row, cols, "sourceSku")),
                _text(get(sheet, row, cols, "warehouseId")), _text(get(sheet, row, cols, "sourceSystem")))
            pid = identity["product"]
            if identity["identityStatus"] != "mapped":
                qualifier = identity["sourceSku"] or identity["specification"]
                if qualifier:
                    pid = raw + " · " + qualifier
                elif identity["identityStatus"] == "ambiguous":
                    pid = raw + " · 待关联"
                if identity["sourceSystem"]:
                    pid += " · " + identity["sourceSystem"]
            identity["analysisId"] = pid
            if identity["identityStatus"] == "ambiguous":
                issues.add("AMBIGUOUS_PRODUCT_IDENTITY", "商品关联缺少唯一规格或仓库", "来源ID存在多个候选，当前行单独列为待核对，不自动选定关联。", "error", sheet.title, "第" + str(row) + "行", pid)
            identity_cache[cache_key] = identity
        return identity_cache[cache_key]

    def ensure(pid):
        if pid not in meta:
            meta[pid] = {"id": pid, "name": pid, "code": "", "pack": None, "warehouse": "", "basePrice": None, "basePriceLarge": None, "sources": {}, "codes": {}, "warehouseStocks": {}, "warehouseSales": {}, "identityLinks": [], "specification": ""}
        return meta[pid]

    def update_meta(pid, sheet, row, cols, role):
        item = ensure(pid)
        identity = identify(sheet, row, cols)
        item["specification"] = identity["specification"] or item["specification"]
        if identity not in item["identityLinks"]:
            item["identityLinks"].append(identity)
        for field in ("name", "code", "warehouse"):
            val = identity["warehouse"] if field == "warehouse" else (identity["name"] if field == "name" and identity["identityStatus"] == "mapped" else _text(get(sheet, row, cols, field)))
            if val:
                if field == "code":
                    if role in ("sales", "outbound"):
                        platform_codes[pid][role] = val
                        continue
                    old = item["code"]
                    if old and old != val:
                        issues.add("CODE_CONFLICT", "商品编码存在冲突", f"商品 {pid} 的来源编码为 {val}，主资料编码为 {old}；本次暂按商品标识关联。", "warning", sheet.title, get_column_letter(cols[field]) + str(row), pid)
                    item["codes"][role] = val
                if not item.get(field) or (field == "name" and item[field] == pid):
                    item[field] = val
                    item["sources"][field] = ref(sheet, row, cols, field)
        for field in ("pack", "basePrice", "basePriceLarge"):
            val = _number(get(sheet, row, cols, field))
            if val is not None and item[field] is None:
                if field == "pack" and val <= 0:
                    issues.add("INVALID_PACK", "箱规应大于零", f"商品 {pid} 的箱规 {val} 无法用于单位换算。", "error", sheet.title, get_column_letter(cols[field]) + str(row), pid)
                    continue
                item[field] = val
                item["sources"][field] = ref(sheet, row, cols, field)
        if item["basePrice"] is None and item["basePriceLarge"] is not None and item["pack"]:
            item["basePrice"] = item["basePriceLarge"] / item["pack"]
        if item["basePriceLarge"] is None and item["basePrice"] is not None and item["pack"]:
            item["basePriceLarge"] = item["basePrice"] * item["pack"]
        return item

    # Process master data before movements regardless of physical sheet order.
    for role in ("products", "conversion", "summary", "inventory"):
        info = mapped(role)
        if not info:
            if role in ("summary", "inventory"):
                issues.add("MISSING_ROLE", f"未配置{ROLE_NAMES[role]}数据", "其余已识别数据仍可生成看盘结果；可在字段映射中指定数据来源。", "info")
            continue
        sheet, hr, cols = info
        count = 0
        seen = set()
        for row in _row_numbers(sheet, hr + 1):
            raw_pid = _text(get(sheet, row, cols, "product"))
            if not raw_pid or _total_label(raw_pid):
                continue
            pid = identify(sheet, row, cols)["analysisId"]
            count += 1
            item = update_meta(pid, sheet, row, cols, role)
            if pid in seen and role in ("products", "conversion", "summary"):
                issues.add("DUPLICATE_PRODUCT", "商品标识重复", f"{ROLE_NAMES[role]}出现重复商品 {pid}，请确认是否需要区分规格。", "error", sheet.title, get_column_letter(cols["product"]) + str(row), pid)
                if role == "summary":
                    # Identical duplicate aliases describe one target. A
                    # conflicting target cannot be resolved by row order.
                    if _number(get(sheet, row, cols, "plan")) != item.get("plan"):
                        item["plan"] = None
                    continue
            seen.add(pid)
            if role == "summary":
                val = _number(get(sheet, row, cols, "plan"))
                if val is not None:
                    item["plan"] = (item.get("plan") or 0) + val
                    item["sources"]["plan"] = ref(sheet, row, cols, "plan")
            if role == "inventory":
                large = _number(get(sheet, row, cols, "stockLarge"))
                small = _number(get(sheet, row, cols, "stockSmall"))
                pack = _number(get(sheet, row, cols, "pack")) or item["pack"]
                if small is None and large is not None and pack:
                    small = large * pack
                elif small is None and large is not None:
                    issues.add("MISSING_PACK", "库存无法换算小单位", f"商品 {pid} 有大单位库存但缺少箱规。", "error", sheet.title, "", pid)
                if large is None and small is not None and pack:
                    large = small / pack
                source_amount = _number(get(sheet, row, cols, "amount"))
                price_large = _number(get(sheet, row, cols, "basePriceLarge"))
                price_small = _number(get(sheet, row, cols, "basePrice"))
                calculated = large * (price_large if price_large is not None else item["basePriceLarge"]) if large is not None and (price_large is not None or item["basePriceLarge"] is not None) else None
                if calculated is None and small is not None and (price_small is not None or item["basePrice"] is not None):
                    calculated = small * (price_small if price_small is not None else item["basePrice"])
                if source_amount is not None and calculated is not None and abs(source_amount - calculated) > .02:
                    issues.add("AMOUNT_MISMATCH", "库存金额与数量不一致", f"商品 {pid} 源金额 {source_amount:g}，按数量与价格计算为 {calculated:g}。", "warning", sheet.title, get_column_letter(cols["amount"]) + str(row), pid)
                amount = source_amount if options["amountMode"] == "source" and source_amount is not None else calculated
                identity = identify(sheet, row, cols)
                location_key = ("id", identity["warehouseId"]) if identity["warehouseId"] else ("name", identity["warehouse"])
                if location_key in item["warehouseStocks"]:
                    issues.add("STOCK_SNAPSHOT_OVERLAP", "同仓库存范围重叠", "同一标准商品与仓库出现多条库存快照，后续重复项未相加，请核对来源。", "error", sheet.title, "第" + str(row) + "行", pid)
                    continue
                stock = item["warehouseStocks"].setdefault(location_key, {
                    "warehouseId": identity["warehouseId"], "warehouse": identity["warehouse"],
                    "warehouseIdentityStatus": "identified" if identity["warehouseId"] else "unknown_id",
                    "stockSmall": None, "stockLarge": None, "stockAmount": None,
                    "snapshotDate": (_date(get(sheet, row, cols, "date"), wb.epoch, year).isoformat() if _date(get(sheet, row, cols, "date"), wb.epoch, year) else None), "sources": []})
                stock["sources"].append({"sheet": sheet.title, "row": row, "identity": identity})
                for metric, value in (("stockLarge", large), ("stockSmall", small), ("stockAmount", amount)):
                    if value is not None:
                        stock[metric] = (stock[metric] or 0) + value
                for key, value in (("stockLarge", large), ("stockSmall", small), ("stockAmount", amount)):
                    if value is not None:
                        item[key] = (item.get(key) or 0) + value
                        item["sources"][key] = ref(sheet, row, cols, "amount" if key == "stockAmount" else key)
        sources.append({"role": role, "label": ROLE_NAMES[role], "sheet": sheet.title, "records": count, "startDate": None, "endDate": None})

    for role in ("sales", "outbound", "orders", "transit", "loss"):
        info = mapped(role)
        role_data[role] = {}
        if not info:
            issues.add("MISSING_ROLE", f"未配置{ROLE_NAMES[role]}数据", "相关指标保留为空，避免将缺少数据误认为零。", "info")
            continue
        sheet, hr, cols = info
        date_cols = _date_columns(sheet, hr, wb.epoch, year)
        date_col = cols.get("date")
        wide = bool(date_cols) and not date_col
        if not wide and not date_col:
            issues.add("MISSING_DATE_COLUMN", "缺少日期字段", f"{ROLE_NAMES[role]}既未识别到日期表头，也未配置长表日期列，相关指标暂不计算。", "error", sheet.title)
            continue
        dates_seen = set(d for _, d in date_cols)
        aux = {}
        aux_col = cols.get("amountProduct")
        if aux_col:
            for row in _row_numbers(sheet, hr + 1):
                pid = _text(sheet.cell(row, aux_col).value)
                if pid and not _total_label(pid):
                    if pid in aux:
                        issues.add("DUPLICATE_AUXILIARY_KEY", "辅助汇总商品重复", f"商品 {pid} 的汇总区存在多个条目，本次不采用该商品汇总区金额。", "error", sheet.title, get_column_letter(aux_col) + str(row), pid)
                        aux[pid] = None
                    else:
                        aux[pid] = row
        grouped = defaultdict(list)
        effective_dates = set()
        count = 0
        product_counts = defaultdict(int)
        for row in _row_numbers(sheet, hr + 1):
            key = _text(get(sheet, row, cols, "product"))
            if key and not _total_label(key):
                product_counts[identify(sheet, row, cols)["analysisId"]] += 1
        for row in _row_numbers(sheet, hr + 1):
            raw_pid = _text(get(sheet, row, cols, "product"))
            if not raw_pid or _total_label(raw_pid):
                continue
            identity = identify(sheet, row, cols)
            pid = identity["analysisId"]
            item = update_meta(pid, sheet, row, cols, role)
            if wide:
                events = []
                for column, d in date_cols:
                    value = sheet.cell(row, column).value
                    qty = _number(value)
                    if qty is not None:
                        events.append((d, qty, sheet.cell(row, column).coordinate))
                    elif value is not None:
                        issues.add("INVALID_QUANTITY", "数量不是有效数值", f"商品 {pid} 的 {d.isoformat()} 数量无法解析。", "error", sheet.title, sheet.cell(row, column).coordinate, pid)
                # Blank order dates denote no report where a numeric total
                # explicitly confirms zero; no such conversion is made for sales.
                total = _number(get(sheet, row, cols, "quantity"))
                if not events and total == 0 and role != "sales":
                    events = [(d, 0., "") for _, d in date_cols if month_start <= d <= cutoff]
            else:
                d = _date(get(sheet, row, cols, "date"), wb.epoch, year)
                qty = _number(get(sheet, row, cols, "quantity"))
                if not d:
                    issues.add("INVALID_DATE", "日期缺失或无法识别", f"商品 {pid} 的日期无效，该行不参与统计。", "error", sheet.title, get_column_letter(date_col) + str(row), pid)
                    continue
                dates_seen.add(d)
                if qty is None:
                    issues.add("INVALID_QUANTITY", "数量缺失或无法识别", f"商品 {pid} 的数量无效，该行不参与统计。", "error", sheet.title, "", pid)
                    continue
                events = [(d, qty, get_column_letter(cols.get("quantity", 1)) + str(row))]
            count += len(events)
            if aux_col:
                amount_row = aux.get(raw_pid)
            else:
                amount_row = row
            def amount_value(field):
                if not amount_row:
                    return None
                if aux_col and product_counts[pid] > 1 and field in ("amount", "supplyAmount"):
                    issues.add("AUXILIARY_SCOPE_AMBIGUOUS", "辅助汇总对应多条商品明细", f"商品 {pid} 有多条明细；辅助汇总金额无法逐行分配，已按明细数量和价格计算，避免重复累计。", "warning", sheet.title, "", pid)
                    return None
                c = cols.get(field)
                return _number(sheet.cell(amount_row, c).value) if c else None
            source_amount = amount_value("amount")
            source_supply = amount_value("supplyAmount")
            supply_price = amount_value("supplyPrice")
            base_price = _number(get(sheet, row, cols, "basePrice"))
            if base_price is None:
                base_price = item["basePriceLarge"] if role in ("orders", "transit") else item["basePrice"]
            pack = item["pack"] if role in ("orders", "transit") else 1.
            valid_events = []
            for d, qty, cell in events:
                adjusted = d
                if role == "sales" and options.get("alignSalesYear") and d.year != year:
                    try:
                        adjusted = d.replace(year=year)
                    except ValueError:
                        continue
                effective_dates.add(adjusted)
                valid_events.append((adjusted, qty, cell))
            raw_total = sum(q for _, q, _ in valid_events)
            calculated_full = raw_total * base_price if base_price is not None else None
            full_supply = raw_total * supply_price if supply_price is not None else None
            amount_cell = get_column_letter(cols["amount"]) + str(amount_row) if cols.get("amount") and amount_row else ""
            if source_amount is not None and calculated_full is not None and abs(source_amount - calculated_full) > .02:
                issues.add("AMOUNT_MISMATCH", f"{ROLE_NAMES[role]}金额与明细不一致", f"商品 {pid} 源金额 {source_amount:g}，明细数量 × 单价为 {calculated_full:g}。当前{('保留源金额' if options['amountMode'] == 'source' else '采用明细重算')}，未改动原文件。", "warning", sheet.title, amount_cell, pid)
            if source_supply is not None and full_supply is not None and abs(source_supply - full_supply) > .02:
                issues.add("AMOUNT_MISMATCH", f"{ROLE_NAMES[role]}供价金额与明细不一致", f"商品 {pid} 源供价金额 {source_supply:g}，数量 × 供价为 {full_supply:g}。", "warning", sheet.title, get_column_letter(cols["supplyAmount"]) + str(amount_row), pid)
            if role == "transit":
                selected = [(d, q, cell) for d, q, cell in valid_events if d <= cutoff]
            else:
                selected = [(d, q, cell) for d, q, cell in valid_events if month_start <= d <= cutoff]
            if any(d > cutoff and qty != 0 for d, qty, _ in valid_events):
                issues.add("FUTURE_DATA", "存在截止日期之后的数据", f"商品 {pid} 在{ROLE_NAMES[role]}中有晚于 {cutoff.isoformat()} 的数量，已排除。", "info", sheet.title, "", pid)
            if role == "transit":
                overdue = [(d, q) for d, q, _ in selected if (cutoff - d).days > transit_days and q != 0]
                if overdue:
                    issues.add("TRANSIT_OVERDUE", "在途记录超过跟踪天数", f"商品 {pid} 有 {sum(q for _, q in overdue):g} 大单位超过 {transit_days} 天；保留为未核销在途，等待到货确认。", "warning", sheet.title, "", pid)
            if not selected:
                continue
            quantity = sum(q for _, q, _ in selected)
            same_scope = len(selected) == len(valid_events)
            amount = quantity * base_price if base_price is not None else None
            supply_amount = quantity * supply_price if supply_price is not None else None
            if options["amountMode"] == "source" and same_scope:
                if source_amount is not None:
                    amount = source_amount
                if source_supply is not None:
                    supply_amount = source_supply
            if not same_scope and (source_amount is not None or source_supply is not None):
                issues.add("SOURCE_SCOPE_FILTERED", "源金额范围大于统计范围", f"商品 {pid} 的{ROLE_NAMES[role]}源金额包含截止日期外或其他月份数据，本次金额按所选明细与单价重算。", "info", sheet.title, amount_cell, pid)
            if pack is None and role in ("orders", "transit"):
                issues.add("MISSING_PACK", "缺少箱规，数量无法统一", f"商品 {pid} 的{ROLE_NAMES[role]}保留原单位，统一小单位数量暂不可用。", "warning", sheet.title, "", pid)
            if role == "sales":
                location_key = ("id", identity["warehouseId"]) if identity["warehouseId"] else ("name", identity["warehouse"])
                location_sales = item["warehouseSales"].setdefault(location_key, defaultdict(float))
                for d, qty, _ in selected:
                    if pack is not None:
                        location_sales[d.isoformat()] += qty * pack
            grouped[pid].append({"quantity": quantity * pack if pack is not None else None,
                                 "rawQuantity": quantity, "amount": amount, "supplyAmount": supply_amount,
                                 "daily": {d.isoformat(): q * pack if pack is not None else None for d, q, _ in selected},
                                 "events": selected, "source": ref(sheet, row, cols, "quantity") if not wide else f"{sheet.title}!第{row}行日期明细",
                                 "amountSource": f"{sheet.title}!{amount_cell}" if options["amountMode"] == "source" and same_scope and source_amount is not None else f"{sheet.title}!第{row}行数量 × 单价"})
        if role == "sales" and dates_seen and any(d.year != year for d in dates_seen):
            issues.add("DATE_YEAR_MISMATCH", "销量年份与统计月份不一致", f"销售表年份包含 {', '.join(map(str, sorted({d.year for d in dates_seen})))}，当前统计 {year} 年。" + ("已按配置将销售年份对齐至统计年份，保留月份与日，属于人工指定的日期调整。" if options.get("alignSalesYear") else "已严格按原日期筛选；如为旧模板日期，可在配置中显式开启年份对齐。"), "warning", sheet.title, f"第{hr}行")
        date_coverage[role] = effective_dates
        for pid, parts in grouped.items():
            daily = defaultdict(float)
            for part in parts:
                for d, qty in part["daily"].items():
                    if qty is not None:
                        daily[d] += qty
            role_data[role][pid] = {"quantity": _sum(p["quantity"] for p in parts), "amount": _sum(p["amount"] for p in parts),
                                   "supplyAmount": _sum(p["supplyAmount"] for p in parts), "daily": dict(daily),
                                   "source": parts[0]["source"], "amountSource": parts[0]["amountSource"]}
        if role != "transit" and count and not grouped:
            issues.add("NO_PERIOD_DATA", f"当前统计范围没有{ROLE_NAMES[role]}记录", f"统计 {month_start.isoformat()} 至 {cutoff.isoformat()}，来源日期未覆盖该期间，相关指标为空。", "warning", sheet.title)
        sources.append({"role": role, "label": ROLE_NAMES[role], "sheet": sheet.title, "records": count,
                        "startDate": min(dates_seen).isoformat() if dates_seen else None, "endDate": max(dates_seen).isoformat() if dates_seen else None})

    for pid, codes in platform_codes.items():
        if len(set(codes.values())) > 1:
            issues.add("PLATFORM_ID_CONFLICT", "同一商品的平台 ID 不一致", f"商品 {pid} 的平台销量 ID 为 {codes.get('sales', '—')}，出库 ID 为 {codes.get('outbound', '—')}；暂按商品标识关联，请核对是否换链接。", "warning", product=pid)
    issues.add("PLAN_BASIS_ASSUMPTION", "计划按单品独立考核", f"当前口径：{ {'orders_amount':'报单金额', 'outbound_amount':'出库金额', 'orders_quantity':'报单小单位数量', 'outbound_quantity':'出库小单位数量', 'sales_quantity':'平台销售数量'}[basis]}，目标 {target:.0%}；逐款计算达标和缺口，超额商品不抵扣其他商品的任务。", "info")
    if not options.get("inventoryComparable"):
        issues.add("INVENTORY_POOL_UNCONFIRMED", "库存与销量是否同一库存池待确认", "库存数量正常展示；可售天数暂留空。确认仓库库存与销量可比较后，可在配置中开启计算。", "info")
    if any(s["role"] == "inventory" for s in sources):
        issues.add("STOCK_SNAPSHOT_DATE", "库存使用导入快照", "库存表没有统一日期筛选；截止日期用于业务明细，不能自动还原历史盘点库存。", "info")

    products = []
    trend = defaultdict(lambda: {"sales": None, "orders": None, "outbound": None})
    remaining = max(0, (month_end - cutoff).days)
    required_sales_days = {cutoff - timedelta(days=i) for i in range(1, average_days + 1)}
    for pid, item in meta.items():
        records = {role: role_data.get(role, {}).get(pid, {}) for role in ("sales", "outbound", "orders", "transit", "loss")}
        sales, outbound, orders, transit, loss = (records[k] for k in ("sales", "outbound", "orders", "transit", "loss"))
        completed_role, completed_type = basis.split("_")
        completed = records[completed_role].get("amount" if completed_type == "amount" else "quantity")
        plan = item.get("plan")
        p = {"id": pid, "name": item["name"], "code": item["code"], "pack": item["pack"], "warehouse": item["warehouse"],
             "plan": plan, "completed": completed, "stockLarge": item.get("stockLarge"), "stockSmall": item.get("stockSmall"), "stockAmount": item.get("stockAmount"),
             "transitSmall": transit.get("quantity"), "transitAmount": transit.get("amount"), "salesQuantity": sales.get("quantity"),
             "yesterdaySales": sales.get("daily", {}).get((cutoff - timedelta(days=1)).isoformat()), "averageSales": None, "coverageDays": None,
             "outboundQuantity": outbound.get("quantity"), "outboundAmount": outbound.get("amount"), "supplyAmount": outbound.get("supplyAmount"),
             "lossAmount": loss.get("amount"), "lossQuantity": loss.get("quantity"), "sources": dict(item["sources"]),
             "specification": item["specification"], "identityLinks": item["identityLinks"], "warehouseStocks": []}
        if required_sales_days and all(d.isoformat() in sales.get("daily", {}) for d in required_sales_days):
            p["averageSales"] = sum(sales["daily"][d.isoformat()] for d in required_sales_days) / average_days
        if sales and p["averageSales"] is None:
            issues.add("INSUFFICIENT_SALES_HISTORY", "近日日均销量数据不足", f"商品 {pid} 缺少截止日前完整 {average_days} 天销量；日均销量与可售天数暂不计算。", "info", product=pid)
        for role, record in records.items():
            for d, qty in record.get("daily", {}).items():
                if role in ("sales", "orders", "outbound"):
                    trend[d][role] = (trend[d][role] or 0) + qty
            if role == completed_role and record.get("amountSource"):
                p["sources"]["completed"] = record["amountSource"] if completed_type == "amount" else record["source"]
            if role == "outbound":
                p["sources"]["outboundAmount"] = record.get("amountSource", "")
                p["sources"]["supplyAmount"] = record.get("source", "")
            if role in ("transit", "loss", "sales"):
                p["sources"][{"transit":"transitAmount", "loss":"lossAmount", "sales":"salesQuantity"}[role]] = record.get("amountSource" if role != "sales" else "source", "")
        overrides = (options.get("overrides") or {}).get(pid, {})
        if not isinstance(overrides, dict):
            raise ValueError("人工调整应按商品标识保存字段对象。")
        for field in ("plan", "stockSmall", "stockAmount", "supplyPrice", "lossAmount"):
            if field not in overrides or overrides[field] is None or overrides[field] == "":
                continue
            number = _number(overrides[field])
            if number is None or number < 0:
                raise ValueError(f"商品 {pid} 的人工调整“{field}”必须为大于等于零的数值。")
            if field == "supplyPrice":
                p["supplyAmount"] = p["outboundQuantity"] * number if p["outboundQuantity"] is not None else None
                p["sources"]["supplyAmount"] = "手动调整：供货单价 × 出库小单位数量"
            else:
                p[field] = number
                p["sources"][field] = "手动调整"
                if field == "stockSmall":
                    p["stockLarge"] = number / item["pack"] if item["pack"] else None
                    if overrides.get("stockAmount") in (None, "") and item["basePrice"] is not None:
                        p["stockAmount"] = number * item["basePrice"]
                        p["sources"]["stockAmount"] = "手动调整库存数量 × 基准小单位价格"
            issues.add("MANUAL_OVERRIDE", "使用人工调整", f"商品 {pid} 的{FIELD_LABELS.get(field, field)}采用人工输入 {number:g}，原文件保持不变。", "info", cell=field, product=pid)
        p["achievement"] = _ratio(p["completed"], p["plan"])
        p.update(_plan_assessment(p["plan"], p["completed"], target, remaining))
        p["discountRate"] = _ratio(p["supplyAmount"], p["outboundAmount"])
        p["markdownRate"] = 1 - p["discountRate"] if p["discountRate"] is not None else None
        p["lossRate"] = _ratio(p["lossAmount"], p["outboundAmount"])
        if options.get("inventoryComparable") and p["stockSmall"] is not None and p["averageSales"] is not None and p["averageSales"] > 0:
            p["coverageDays"] = p["stockSmall"] / p["averageSales"]
            if p["coverageDays"] < transit_days:
                issues.add("LOW_STOCK", "现货覆盖天数较短", f"商品 {pid} 现货预计覆盖 {p['coverageDays']:.1f} 天，短于当前跟踪天数 {transit_days} 天，请确认货源和补货安排。", "warning", product=pid)
        for location_key, raw_stock in item["warehouseStocks"].items():
            stock = copy.deepcopy(raw_stock)
            stock.update(averageSales=None, coverageDays=None, salesBasis="mapped_sales")
            daily_sales = item["warehouseSales"].get(location_key, {})
            complete = all(day.isoformat() in daily_sales for day in required_sales_days)
            if options.get("inventoryComparable") and complete:
                stock["averageSales"] = sum(daily_sales[day.isoformat()] for day in required_sales_days) / average_days
                if stock["stockSmall"] is not None and stock["averageSales"] > 0:
                    stock["coverageDays"] = stock["stockSmall"] / stock["averageSales"]
            stock["coverageReason"] = "按同仓完整销量计算" if stock["coverageDays"] is not None else "同仓库存池或完整销量待确认"
            p["warehouseStocks"].append(stock)
        p["warehouseStocks"].sort(key=lambda row: (row["warehouseId"], row["warehouse"]))
        if p["warehouseStocks"]:
            p["warehouse"] = "、".join(dict.fromkeys(row["warehouse"] or "未指定仓库" for row in p["warehouseStocks"]))
        # A total stock divided by unallocated platform sales is not warehouse
        # coverage. Preserve old single-pool behavior only for one stock pool.
        if len(p["warehouseStocks"]) > 1:
            p["coverageDays"] = None
        products.append(p)

    # Scan source errors without treating a cached result as a working formula.
    used_sheets = {s["sheet"] for s in sources}
    for name in used_sheets:
        for c in _nonempty(wb[name]):
            if c.data_type == "e":
                issues.add("SOURCE_FORMULA_ERROR", "原表包含公式错误", f"{name}!{c.coordinate} 保存值为 {c.value}；看盘指标按明细和配置独立计算。", "warning", name, c.coordinate)
    for p in products:
        own = [i for i in issues.items if i["product"] == p["id"]]
        p["issues"] = own
        p["status"] = "error" if any(i["severity"] == "error" for i in own) else "warning" if any(i["severity"] == "warning" for i in own) else "normal"
    summary = {key: _sum(p[key] for p in products) for key in ("plan", "completed", "stockAmount", "transitAmount", "outboundAmount", "supplyAmount", "lossAmount", "salesQuantity")}
    known_gap = _sum(p["targetGap"] for p in products)
    unknown_count = sum(p["assessmentStatus"] == "unknown" for p in products)
    known_daily = known_gap / remaining if known_gap is not None and remaining else (0. if known_gap == 0 else None)
    summary.update({"achievement": _ratio(summary["completed"], summary["plan"]), "targetRate": target,
                    "assessmentScope": "product", "assessmentVersion": 1, "remainingDays": remaining,
                    "metCount": sum(p["assessmentStatus"] == "met" for p in products),
                    "belowTargetCount": sum(p["assessmentStatus"] == "below" for p in products),
                    "unknownTargetCount": unknown_count,
                    "knownTargetGap": known_gap, "targetGap": None if unknown_count else known_gap,
                    "knownDailyRequired": known_daily, "dailyRequired": None if unknown_count else known_daily,
                    "discountRate": _ratio(summary["supplyAmount"], summary["outboundAmount"]), "lossRate": _ratio(summary["lossAmount"], summary["outboundAmount"]),
                    "productCount": len(products), "issueCount": len(issues.items),
                    "partialMetrics": [key for key in ("plan", "completed", "stockAmount", "outboundAmount", "lossAmount") if any(p[key] is None for p in products)]})
    # Ratios need matching populations. A known numerator must not be divided by
    # a denominator that silently excludes products with missing source data.
    if any(p["plan"] is None or p["plan"] <= 0 or p["completed"] is None for p in products):
        summary["achievement"] = None
        summary["targetGap"] = None
    if any(p["outboundAmount"] is None or p["supplyAmount"] is None for p in products):
        summary["discountRate"] = None
    if any(p["outboundAmount"] is None or p["lossAmount"] is None for p in products):
        summary["lossRate"] = None
    summary["markdownRate"] = 1 - summary["discountRate"] if summary["discountRate"] is not None else None
    return {"filename": Path(path).name, "asOf": asof.isoformat(), "period": options["period"], "config": options, "summary": summary,
            "products": products, "issues": issues.items, "trend": [{"date": d, **vals} for d, vals in sorted(trend.items())], "sources": sources}
