"""Value-only operational imports. Inputs are never modified.

Summary observations deliberately remain separate from shipment/aftersale facts.
Mappings accept Excel column letters or exact header labels and explicit constants.
"""
from __future__ import annotations

from datetime import date, datetime
import hashlib
import math
from pathlib import Path
import re

import openpyxl
from openpyxl.utils import column_index_from_string
from openpyxl.utils.datetime import from_excel


KINDS = {"summary", "shipments", "aftersales", "loss"}
MAX_RECORDS = 20000


def text(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def number(value):
    """Return quantity and original unit; '-' is missing, never zero."""
    if value is None or text(value) in {"", "-", "—", "--"}:
        return None, ""
    if isinstance(value, bool):
        raise ValueError("数量或金额不能是布尔值。")
    raw = re.sub(r"[,，\s￥¥]", "", text(value))
    match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))(.*)", raw)
    if not match:
        raise ValueError("数量或金额不是有效数字。")
    result = float(match.group(1))
    if not math.isfinite(result):
        raise ValueError("数量或金额必须是有限数字。")
    suffix = match.group(2)
    if suffix == "%":
        return result / 100, "ratio"
    if suffix and not re.fullmatch(r"[\u4e00-\u9fffA-Za-z/]+", suffix):
        raise ValueError("无法识别数量单位。")
    return result, suffix


def full_date(value, epoch=None):
    if value in (None, "", "-", "—"):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)) and not isinstance(value, bool) and epoch and 30000 < value < 80000:
        return from_excel(value, epoch).date().isoformat()
    match = re.fullmatch(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?(?:[ T].*)?", text(value))
    if not match:
        match = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", text(value))
    if not match:
        raise ValueError("日期必须有原表中的完整年月日，不能由文件名或今天补齐。")
    try:
        return date(*map(int, match.groups())).isoformat()
    except ValueError:
        raise ValueError("业务日期无效。") from None


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _norm(value):
    return re.sub(r"[\s（）()_：:]", "", text(value)).lower()


ALIASES = {
    "businessId": ("业务明细ID", "发货明细ID", "售后ID", "售后单号", "明细ID", "businessId"),
    "orderId": ("订单ID", "订单号", "orderId"),
    "lineId": ("订单明细ID", "子订单号", "lineId"),
    "shipmentId": ("原发货明细ID", "关联发货ID", "shipmentId"),
    "groupId": ("售后关联组ID", "扣减组ID", "退货退款关联ID", "groupId", "caseId"),
    "product": ("商品ID", "商品编码", "产品代码", "product"),
    "productInfo": ("商品信息",),
    "sku": ("SKU", "SKU ID", "规格ID", "sourceSku"),
    "name": ("商品名称", "产品名称", "name"),
    "specification": ("商品规格", "规格", "specification"),
    "warehouse": ("仓库名称", "仓库", "仓库信息", "仓组", "warehouse"),
    "warehouseId": ("仓库ID", "warehouseId"),
    "shop": ("店铺", "店铺名称", "卖场名称", "销售区域", "shop"),
    "date": ("发货日期", "售后发生日期", "销售日期", "业务日期", "日期", "date"),
    "originalDate": ("原销售日期", "原发货日期", "originalDate"),
    "statusDate": ("状态日期", "状态生效日期", "statusDate"),
    "quantity": ("发货数量", "售后数量", "商品数量", "数量", "quantity"),
    "unit": ("数量单位", "单位", "unit"),
    "baseUnit": ("统一单位", "baseUnit"),
    "pack": ("箱规", "换算率", "pack"),
    "status": ("发货状态", "售后状态", "状态", "status"),
    "type": ("售后类型", "扣减类型", "type"),
    "registered": ("登记货损", "登记货损数量", "registered"),
    "opening": ("期初", "期初数量", "opening"),
    "inbound": ("入库", "入库数量", "inbound"),
    "outbound": ("出库", "出库数量", "outbound"),
    "physical": ("实盘", "实盘数量", "physical"),
    "unitPrice": ("计价单价", "unitPrice"),
    "registeredAmount": ("登记货损金额", "registeredAmount"),
    "periodStart": ("期间开始", "periodStart"),
    "periodEnd": ("期间结束", "periodEnd"),
}


def normalize_record(raw, kind, context=None):
    if not isinstance(raw, dict):
        raise ValueError("业务记录必须是对象。")
    context = context or {}
    record = dict(raw)
    record["kind"] = kind
    for field in ("businessId", "orderId", "lineId", "shipmentId", "groupId", "product", "sku", "name",
                  "specification", "warehouse", "warehouseId", "shop", "status", "type", "unit", "baseUnit"):
        value = raw.get(field, "")
        if field in {"businessId", "product", "sku", "orderId", "lineId", "shipmentId"} and isinstance(value, (int, float)) and abs(value) >= 10**15:
            raise ValueError(f"{field}为长数字，原表须按文本保存以免精度丢失。")
        record[field] = text(value)
    record["sourceSystem"] = text(raw.get("sourceSystem") or context.get("sourceSystem"))
    if not record["product"] and raw.get("productInfo"):
        # IDs extracted from description remain explicitly marked in the preview.
        matches = re.findall(r"(?:商品\s*[Ii][Dd]|[Ii][Dd]|商品编码)\s*[:：=]?\s*([A-Za-z0-9_-]+)", text(raw["productInfo"]))
        if not matches:
            prefix = re.match(r"^\s*(\d{6,})\s*(?:[+＋]|\n|$)", text(raw["productInfo"]))
            matches = [prefix.group(1)] if prefix else []
        if len(set(matches)) == 1:
            record["product"] = matches[0]
            record["extractedProduct"] = True
        record["productInfo"] = text(raw["productInfo"])
    if not record["product"]:
        raise ValueError("缺商品ID；可在映射中指定商品列或显式常量。")
    if any(len(record.get(field, "")) > 500 for field in ("product", "businessId", "warehouse", "name", "specification")):
        raise ValueError("业务标识或名称过长。")
    for field in ("date", "originalDate", "statusDate", "periodStart", "periodEnd"):
        record[field] = full_date(raw.get(field))
    if kind == "shipments" and record.get("originalDate") is None and raw.get("originalDateIsShipmentDate") is True:
        record["originalDate"] = record.get("date")
    if kind in {"shipments", "aftersales"}:
        if not record["businessId"]:
            raise ValueError("事件导入必须有稳定业务明细ID，不能用商品和日期代替。")
        quantity, suffix = number(raw.get("quantity"))
        if quantity is not None and quantity < 0:
            raise ValueError("原始事件数量须非负；冲减方向由售后事件表达。")
        if suffix and record["unit"] and suffix != record["unit"]:
            raise ValueError("事件数量自带单位与指定单位不一致，请先明确换算，不能覆盖原始单位。")
        record["quantity"] = quantity
        record["unit"] = record["unit"] or suffix
        pack, _ = number(raw.get("pack"))
        if pack is not None and pack <= 0:
            raise ValueError("单位换算率须大于零。")
        record["pack"] = pack
        record["conversionVersion"] = text(raw.get("conversionVersion") or context.get("conversionVersion"))
        # No implicit box-to-piece conversion. Explicit baseUnit declares comparable units.
        if quantity is None or not record["unit"] or not record["baseUnit"]:
            record["normalizedQuantity"] = None
        elif record["unit"] == record["baseUnit"]:
            record["normalizedQuantity"] = quantity
        elif pack is not None:
            record["normalizedQuantity"] = quantity * pack
        else:
            record["normalizedQuantity"] = None
        if record["normalizedQuantity"] is not None and not math.isfinite(record["normalizedQuantity"]):
            raise ValueError("数量换算超出有效范围。")
        state_map = context.get("statusMap") or {}
        record["effectiveState"] = text(raw.get("effectiveState") or state_map.get(record["status"]))
    elif kind == "loss":
        quantity_fields = {"registered", "opening", "inbound", "outbound", "physical"}
        declared_unit = record["unit"]
        field_units = {}
        prior_units = raw.get("quantityUnits") or {}
        if not isinstance(prior_units, dict) or set(prior_units) - quantity_fields:
            raise ValueError("货损字段单位记录无效。")
        price_basis = text(raw.get("unitPriceBasis"))
        for field in ("registered", "opening", "inbound", "outbound", "physical", "unitPrice", "registeredAmount"):
            record[field], suffix = number(raw.get(field))
            if field == "unitPrice" and suffix:
                price_basis = suffix
            if field == "registeredAmount" and suffix and suffix not in {"元", "人民币", "CNY", "RMB"}:
                raise ValueError("登记金额应采用元口径，其他币种不能直接核对。")
            if field in quantity_fields and record[field] is not None:
                suffix = suffix or text(prior_units.get(field))
                if suffix and declared_unit and suffix != declared_unit:
                    raise ValueError("货损数量自带单位与统一单位不一致；不能直接混加或自动套用箱规。")
                field_units[field] = suffix or declared_unit
        known_units = {unit for unit in field_units.values() if unit}
        if len(known_units) > 1:
            raise ValueError("货损各数量字段单位不一致；请先统一单位后重新预览。")
        record["quantityUnits"] = field_units
        # A suffix on one field does not prove the unit of another bare field.
        # Keep the row uncomputed unless a global declaration or every present
        # quantity establishes the same unit.
        record["unit"] = declared_unit or (next(iter(known_units)) if known_units and all(field_units.values()) else "")
        if price_basis:
            parts = price_basis.split("/")
            if parts[0] not in {"元", "人民币", "CNY", "RMB"} or len(parts) > 2:
                raise ValueError("计价单价需采用元/统一数量单位。")
            if len(parts) == 2 and (not record["unit"] or parts[1] != record["unit"]):
                raise ValueError("计价单价分母与货损数量单位不一致，不能直接计算金额。")
        record["unitPriceBasis"] = price_basis
        if any(record.get(field) is not None and record[field] < 0 for field in ("opening", "inbound", "outbound", "physical", "unitPrice")):
            raise ValueError("期初、入库、出库、实盘和计价单价须非负；调整方向需单独核对。")
        record["confirmationStatus"] = text(raw.get("confirmationStatus")) or "pending"
        record["confirmationNote"] = text(raw.get("confirmationNote"))
        if record["confirmationStatus"] not in {"pending", "confirmed", "recheck"}:
            raise ValueError("货损处理状态应为pending、confirmed或recheck。")
        if record["confirmationStatus"] == "confirmed" and not record["confirmationNote"]:
            raise ValueError("确认货损差异须填写处理说明，原始两轨数值仍保留。")
        record["periodStart"] = record["periodStart"] or record["date"]
        record["periodEnd"] = record["periodEnd"] or record["date"]
        if record["periodStart"] and record["periodEnd"] and record["periodStart"] > record["periodEnd"]:
            raise ValueError("货损期间开始不能晚于结束。")
    return record


def _column(value, headers):
    if value is None or value == "":
        return None
    if isinstance(value, int) and 1 <= value <= 512:
        return value
    if text(value) in headers:
        return headers[text(value)]
    if re.fullmatch(r"[A-Za-z]{1,3}", text(value)):
        result = column_index_from_string(text(value).upper())
        if result <= 512:
            return result
    raise ValueError(f"无法定位列：{value}")


def _merged(sheet, row, column):
    if not column:
        return None, row
    for merged in sheet.merged_cells.ranges:
        if merged.min_row <= row <= merged.max_row and merged.min_col <= column <= merged.max_col:
            return sheet.cell(merged.min_row, merged.min_col).value, merged.min_row
    return sheet.cell(row, column).value, row


def _summary_layout(headers):
    names = {_norm(h) for h in headers}
    if any("商品货款总额" in h for h in names):
        return "settlement"
    if any("售后数" in h or "售后率" in h for h in names):
        return "aftersales_summary"
    if any("仓库" in h and ("库存" in h or "销售" in h) for h in names):
        return "warehouse_summary"
    return None


def _summary_row(sheet, row, headers, columns, constants, epoch, layout):
    record = dict(constants)
    anchors = []
    for field, column in columns.items():
        value, anchor = _merged(sheet, row, column)
        record[field] = value
        if field in {"product", "productInfo", "date"}:
            anchors.append(anchor)
    record["date"] = full_date(record.get("date"), epoch)
    record["layout"] = layout
    record["metrics"], record["quotes"] = {}, []
    for title, column in headers.items():
        value, anchor = _merged(sheet, row, column)
        norm = _norm(title)
        if any(key in norm for key in ("数量", "库存", "销量", "销售数", "售后数", "退款数", "差异量", "货款", "到账", "优惠", "金额", "服务费", "售后率", "退款率")):
            try:
                amount, unit = number(value)
            except ValueError:
                continue
            title_unit = re.search(r"[（(]([^）)]+)[）)]", title)
            unit = unit or (title_unit.group(1) if title_unit else "")
            record["metrics"][title] = {"value": amount, "unit": unit, "column": column, "anchorRow": anchor}
        if "报价" in norm:
            amount, _ = number(sheet.cell(row, column).value)
            if amount is not None:
                record["quotes"].append({"value": amount, "row": row, "column": column})
    record["businessAnchor"] = min(anchors) if anchors else row
    # Multiple quote rows under merged product/date are one observation, not two sales.
    record["rawFields"] = {title: text(_merged(sheet, row, column)[0]) for title, column in headers.items()}
    if layout == "settlement":
        price = next((number(_merged(sheet, row, c)[0])[0] for h, c in headers.items() if "商家报价" in _norm(h)), None)
        quantity = next((number(_merged(sheet, row, c)[0])[0] for h, c in headers.items() if _norm(h) == "商品数量"), None)
        amount = next((number(_merged(sheet, row, c)[0])[0] for h, c in headers.items() if "商品货款总额" in _norm(h)), None)
        record["quantityPriceDifference"] = round(amount - price * quantity, 8) if all(v is not None for v in (price, quantity, amount)) else None
    if layout == "aftersales_summary":
        metrics = record["metrics"]
        denominator = next((v["value"] for k, v in metrics.items() if _norm(k).startswith("销售数")), None)
        checks = []
        for label, numerator_prefix in (("售后率", "售后数"), ("品质退款率", "品质退款数")):
            numerator = next((v["value"] for k, v in metrics.items() if _norm(k).startswith(numerator_prefix)), None)
            reported = next((v["value"] for k, v in metrics.items() if _norm(k) == label), None)
            calculated = numerator / denominator if denominator is not None and denominator > 0 and numerator is not None else None
            checks.append({"metric": label, "reported": reported, "recalculated": calculated,
                           "difference": round(reported - calculated, 10) if reported is not None and calculated is not None else None})
        record["ratioChecks"] = checks
    return record


def parse_import(entry, body):
    kind = text(body.get("kind") or "summary")
    if kind not in KINDS:
        raise ValueError("未知业务导入类型。")
    if entry is None:
        rows = body.get("records", [])
        if not isinstance(rows, list) or len(rows) > MAX_RECORDS:
            raise ValueError("单次记录过多或格式不正确。")
        records, issues = [], []
        for index, row in enumerate(rows, 1):
            try:
                item = normalize_record(row, kind, body)
                item["source"] = {"filename": "手工业务输入", "row": index, "manual": True}
                records.append(item)
            except ValueError as error:
                issues.append({"row": index, "message": str(error)})
        return {"kind": kind, "records": records, "issues": issues, "source": None}
    path = Path(entry["path"])
    digest = file_sha(path)
    mapping = body.get("mapping") or {}
    if not isinstance(mapping, dict):
        raise ValueError("列映射必须为对象。")
    workbook = openpyxl.load_workbook(path, data_only=True)
    records, issues = [], []
    try:
        names = [mapping["sheet"]] if mapping.get("sheet") else workbook.sheetnames
        for sheet_name in names:
            if sheet_name not in workbook.sheetnames:
                raise ValueError("所选工作表不存在。")
            sheet = workbook[sheet_name]
            if sheet.max_column > 512 or sheet.max_row > MAX_RECORDS + 100 or sheet.max_row * sheet.max_column > 500000:
                raise ValueError("工作表过大，请分批导入。")
            header = int(mapping.get("headerRow", 1))
            start = int(mapping.get("dataStartRow", header + 1))
            if not 1 <= header < start <= 1000:
                raise ValueError("请检查表头和起始行。")
            headers = {text(sheet.cell(header, c).value): c for c in range(1, sheet.max_column + 1) if sheet.cell(header, c).value is not None}
            columns = {}
            for field, aliases in ALIASES.items():
                column = next((c for h, c in headers.items() if _norm(h) in {_norm(a) for a in aliases}), None)
                if column:
                    columns[field] = column
            for field, column in (mapping.get("columns") or {}).items():
                columns[field] = _column(column, headers)
            constants = dict(mapping.get("constants") or {})
            layout = mapping.get("layout") or _summary_layout(headers) if kind == "summary" else None
            if kind == "summary" and not layout:
                issues.append({"sheet": sheet_name, "message": "未识别为已支持的汇总结构，请指定工作表及映射。"})
                continue
            groups = {}
            for row in range(start, sheet.max_row + 1):
                if not any(sheet.cell(row, c).value is not None for c in range(1, sheet.max_column + 1)):
                    continue
                try:
                    if kind == "summary":
                        raw = _summary_row(sheet, row, headers, columns, constants, workbook.epoch, layout)
                    else:
                        raw = dict(constants)
                        for field, column in columns.items():
                            raw[field] = _merged(sheet, row, column)[0]
                        for field in ("date", "originalDate", "statusDate", "periodStart", "periodEnd"):
                            raw[field] = full_date(raw.get(field), workbook.epoch)
                    if text(raw.get("product")) in {"合计", "总计", "小计"}:
                        continue
                    record = normalize_record(raw, kind, body)
                    record["source"] = {"filename": entry.get("filename") or path.name, "sha256": digest, "sheet": sheet_name, "row": row}
                    if kind == "summary":
                        key = (record["businessAnchor"], record["product"], record["specification"], record.get("date"))
                        if key in groups:
                            if groups[key]["metrics"] != record["metrics"]:
                                # A merged name/date alone does not establish that
                                # unmerged quantities are one fact.
                                raise ValueError("商品合并组内数量并未共同合并，需明确业务行映射；不能仅按商品合并数量。")
                            groups[key]["quotes"].extend(record["quotes"])
                            groups[key]["source"].setdefault("quoteRows", []).append(row)
                            continue
                        groups[key] = record
                        if record.get("date") is None:
                            issues.append({"sheet": sheet_name, "row": row, "code": "MISSING_PERIOD", "severity": "warning",
                                           "message": "汇总没有业务日期/统计期间；保留独立观察，不进入期间净销量。"})
                    records.append(record)
                except (ValueError, TypeError) as error:
                    issues.append({"sheet": sheet_name, "row": row, "message": str(error)})
            if len(records) > MAX_RECORDS:
                raise ValueError("单次最多导入20,000条业务记录。")
        if file_sha(path) != digest:
            raise ValueError("读取期间原表已变化，请重新预览。")
        return {"kind": kind, "records": records, "issues": issues,
                "source": {"path": str(path.resolve()), "sha256": digest, "filename": entry.get("filename") or path.name}}
    finally:
        workbook.close()
