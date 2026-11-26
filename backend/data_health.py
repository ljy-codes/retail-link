"""Build a user-facing data coverage report from a saved analysis snapshot.

The report is deliberately derived from persisted results and source manifests. It
does not infer missing business facts as zero and does not re-read or mutate an
Excel source. The UI can use it as a compact "what is usable / what is missing"
summary before showing the detailed dashboard.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import math


ROLE_LABELS = {
    "summary": "月计划",
    "products": "商品规格",
    "conversion": "商品对照",
    "inventory": "现货库存",
    "sales": "平台销量",
    "outbound": "仓库出库",
    "orders": "报单记录",
    "transit": "在途记录",
    "loss": "货损记录",
}

METRIC_LABELS = {
    "achievement": "逐款目标考核",
    "stock": "现货库存",
    "stockCoverage": "现货可售天数",
    "netSales": "业务净销量",
    "loss": "货损双轨核对",
}


def _text(value):
    return "" if value is None else str(value).strip()


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _date(value):
    text = _text(value)
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        return None


def _period_bounds(snapshot, observed):
    """Return the requested period and the observed date span without guessing."""
    period = _text(snapshot.get("period"))
    as_of = _date(snapshot.get("asOf"))
    start = None
    if len(period) == 7:
        try:
            year, month = map(int, period.split("-"))
            start = date(year, month, 1)
        except (TypeError, ValueError):
            start = None
    if start is None and observed:
        start = min(observed)
    end = as_of or (max(observed) if observed else None)
    if start and end and end < start:
        start = end
    return start, end


def _source_files(snapshot):
    manifest = snapshot.get("importManifest") or {}
    files = manifest.get("inputFiles") or []
    result = []
    if not files:
        source = snapshot.get("sourceInfo") or {}
        if source:
            files = [source]
    for item in files:
        if isinstance(item, str):
            result.append({"filename": item, "path": item, "sha256": None})
            continue
        if not isinstance(item, dict):
            continue
        result.append({"filename": _text(item.get("filename") or item.get("name")),
                       "path": _text(item.get("path")),
                       "sha256": _text(item.get("sha256")) or None})
    # Keep the report stable when an input manifest repeats a source.
    seen, unique = set(), []
    for item in result:
        key = (item["path"], item["filename"], item["sha256"])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _observed_dates(snapshot, sources):
    dates = set()
    for item in snapshot.get("trend") or []:
        day = _date(item.get("date")) if isinstance(item, dict) else None
        if day:
            dates.add(day)
    for item in sources:
        if not isinstance(item, dict):
            continue
        for field in ("startDate", "endDate"):
            day = _date(item.get(field))
            if day:
                dates.add(day)
    operations = snapshot.get("operationsSnapshot") or {}
    for declaration in (operations.get("coverage") or {}).get("declarations") or []:
        for field in ("start", "end"):
            day = _date(declaration.get(field)) if isinstance(declaration, dict) else None
            if day:
                dates.add(day)
    return sorted(dates)


def _warehouses(snapshot):
    values = set()
    for product in snapshot.get("products") or []:
        if not isinstance(product, dict):
            continue
        for key in ("warehouse", "warehouseId", "shop"):
            value = _text(product.get(key))
            if value:
                values.add(value)
        for stock in product.get("warehouseStocks") or []:
            if not isinstance(stock, dict):
                continue
            value = _text(stock.get("warehouseId") or stock.get("warehouse") or stock.get("shop"))
            if value:
                values.add(value)
    return sorted(values)


def _source_rows(snapshot, observed):
    rows = []
    source_items = snapshot.get("sources") or []
    if not isinstance(source_items, list):
        source_items = []
    for source in source_items:
        if not isinstance(source, dict):
            continue
        role = _text(source.get("role")) or "unknown"
        records = source.get("records")
        records_ok = isinstance(records, int) and records >= 0
        start = _date(source.get("startDate"))
        end = _date(source.get("endDate"))
        if not start and observed:
            start = observed[0]
        if not end and observed:
            end = observed[-1]
        if records_ok and records == 0:
            status = "empty"
            reason = "已识别来源，但当前范围没有可用记录。"
        elif not records_ok or not start or not end:
            status = "unknown"
            reason = "缺少记录数或完整日期范围。"
        else:
            status = "observed"
            reason = "已观察到记录；是否覆盖完整业务期间需结合来源声明确认。"
        rows.append({"role": role, "label": ROLE_LABELS.get(role, role),
                     "sheet": _text(source.get("sheet")), "records": records,
                     "startDate": start.isoformat() if start else None,
                     "endDate": end.isoformat() if end else None,
                     "status": status, "reason": reason})
    return rows


def _missing_fields(snapshot):
    fields = {
        "plan": 0, "completed": 0, "stockSmall": 0, "salesQuantity": 0,
        "outboundQuantity": 0, "lossAmount": 0,
    }
    product_count = 0
    for product in snapshot.get("products") or []:
        if not isinstance(product, dict):
            continue
        product_count += 1
        for field in fields:
            if product.get(field) is None:
                fields[field] += 1
    return {key: {"missing": count, "total": product_count,
                  "status": "missing" if count == product_count and product_count else
                  "partial" if count else "complete"}
            for key, count in fields.items()}


def _metric(key, value, status, reason, missing=None):
    return {"key": key, "label": METRIC_LABELS[key], "value": value,
            "status": status, "reason": reason, "missing": missing or []}


def build_health_report(snapshot):
    """Return a deterministic, JSON-safe coverage report for *snapshot*.

    ``snapshot`` is expected to be a saved history result. The function accepts
    partial snapshots so old history remains viewable while clearly marking
    unavailable metrics.
    """
    if not isinstance(snapshot, dict):
        raise ValueError("分析快照格式无效。")
    snapshot = deepcopy(snapshot)
    sources = _source_files(snapshot)
    observed = _observed_dates(snapshot, snapshot.get("sources") or [])
    start, end = _period_bounds(snapshot, observed)
    missing = _missing_fields(snapshot)
    summary = snapshot.get("summary") or {}
    operations = snapshot.get("operationsSnapshot") or {}
    operation_coverage = operations.get("coverage") or {}
    operation_status = operation_coverage.get("status") or {}
    operation_complete = operations.get("complete") is True
    issues = []
    for issue in snapshot.get("issues") or []:
        if isinstance(issue, dict):
            issues.append(deepcopy(issue))
    for issue in operations.get("issues") or []:
        if isinstance(issue, dict):
            item = deepcopy(issue)
            item.setdefault("severity", "warning")
            item.setdefault("title", item.get("code") or "经营核对待处理")
            item.setdefault("detail", item.get("message") or "经营核对存在待处理项。")
            issues.append(item)

    missing_plan = [field for field, item in missing.items() if item["status"] != "complete"]
    assessment_missing = [field for field in ("plan", "completed")
                          if missing[field]["status"] != "complete"]
    metrics = []
    achievement = summary.get("achievement")
    if _finite(achievement) and not assessment_missing:
        metrics.append(_metric("achievement", achievement, "available", "计划与完成额在相同商品范围内可计算。", assessment_missing))
    elif _finite(summary.get("knownTargetGap")):
        metrics.append(_metric("achievement", None, "partial", "部分商品缺少计划或完成额，完整达成率保留为空。", assessment_missing))
    else:
        metrics.append(_metric("achievement", None, "blocked", "缺少计划或完成额，不能把未知当作0。", assessment_missing))

    stock_value = summary.get("stockAmount")
    stock_missing = [field for field in ("stockSmall",) if missing[field]["status"] != "complete"]
    metrics.append(_metric("stock", stock_value,
                           "available" if _finite(stock_value) and not stock_missing else "partial" if _finite(stock_value) else "blocked",
                           "已显示现货库存；缺失商品仍需补齐。" if stock_missing else "现货库存已识别。", stock_missing))

    coverage_days = [p.get("coverageDays") for p in snapshot.get("products") or [] if isinstance(p, dict)]
    known_days = sum(_finite(value) for value in coverage_days)
    if known_days and known_days == len(coverage_days):
        metrics.append(_metric("stockCoverage", known_days, "available", "库存与同仓完整销量已满足可售天数计算条件。"))
    elif known_days:
        metrics.append(_metric("stockCoverage", known_days, "partial", "只有部分商品具备同仓完整销量，其他商品的可售天数保持未知。"))
    else:
        metrics.append(_metric("stockCoverage", None, "blocked", "库存池或连续销量覆盖尚未确认，不能计算可售天数。", ["inventoryComparable", "same_pool_sales"]))

    net_value = None
    net_status = "blocked"
    if operations:
        if operation_complete and operation_status.get("shipments") and operation_status.get("aftersales"):
            net_status, net_value = "available", "已确认范围的业务净销量"
        elif operations.get("items"):
            net_status, net_value = "partial", "已观察净销量（不代表完整净销量）"
    metrics.append(_metric("netSales", net_value, net_status,
                           "发货与售后事件、覆盖声明和状态均已满足。" if net_status == "available" else
                           "已有发货/售后观察值，但覆盖或明细仍待核对。" if net_status == "partial" else
                           "缺少可关联的发货/售后事实，不能称为完整净销量。", ["shipments", "aftersales"] if net_status != "available" else []))

    loss_rate = summary.get("lossRate")
    loss_source = any(row.get("role") == "loss" for row in _source_rows(snapshot, observed))
    metrics.append(_metric("loss", loss_rate,
                           "available" if _finite(loss_rate) and loss_source else "partial" if loss_source else "blocked",
                           "货损率可在来源和出库范围匹配时查看。" if _finite(loss_rate) and loss_source else
                           "已识别货损来源，但部分字段缺失或期间未完整覆盖。" if loss_source else
                           "尚未识别货损来源。", ["loss_source"] if not loss_source else []))

    blocked = sum(metric["status"] == "blocked" for metric in metrics)
    partial = sum(metric["status"] == "partial" for metric in metrics)
    status = "blocked" if blocked >= 3 else "partial" if blocked or partial or issues else "ready"
    actions = []
    if net_status != "available":
        actions.append({"key": "operations_facts", "label": "补齐发货与售后明细，并确认覆盖期间"})
    if not loss_source or any(metric["key"] == "loss" and metric["status"] != "available" for metric in metrics):
        actions.append({"key": "loss_ledger", "label": "补齐同仓同期的期初、入库、出库、实盘和计价依据"})
    if missing_plan:
        actions.append({"key": "product_fields", "label": "补齐商品计划、完成额或库存字段"})
    if issues:
        actions.append({"key": "review_issues", "label": "处理待核对项后重新分析"})
    if not actions:
        actions.append({"key": "export_evidence", "label": "导出本次分析证据并保留来源哈希"})

    observed_text = [item.isoformat() for item in observed]
    date_gaps = []
    if start and end and observed:
        cursor = start
        observed_set = set(observed)
        while cursor <= end:
            if cursor not in observed_set:
                date_gaps.append(cursor.isoformat())
            cursor += timedelta(days=1)
    return {
        "version": 1,
        "status": status,
        "snapshotId": _text(snapshot.get("id")),
        "source": {"filename": _text(snapshot.get("filename")),
                    "path": _text((snapshot.get("sourceInfo") or {}).get("path")),
                    "sha256": _text((snapshot.get("sourceInfo") or {}).get("sha256")) or None,
                    "files": sources},
        "scope": {"period": _text(snapshot.get("period")),
                  "asOf": _text(snapshot.get("asOf")) or None,
                  "start": start.isoformat() if start else None,
                  "end": end.isoformat() if end else None,
                  "productCount": len(snapshot.get("products") or []),
                  "warehouses": _warehouses(snapshot),
                  "observedDates": observed_text,
                  "observedDays": len(observed),
                  "dateGaps": date_gaps},
        "sources": _source_rows(snapshot, observed),
        "coverage": {"operations": deepcopy(operation_status),
                      "complete": operation_complete,
                      "declarations": deepcopy(operation_coverage.get("declarations") or [])},
        "missing": missing,
        "metrics": metrics,
        "issues": issues,
        "issueCount": len(issues),
        "nextActions": actions,
    }

