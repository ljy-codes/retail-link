"""Reviewed operational facts, dual-period net quantities and stock discrepancies.

One atomic versioned JSON is the persistence boundary. Preview tokens are memory
only; commits recheck both source bytes and the exact persisted generation.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid

import openpyxl

from .operations_import import KINDS, file_sha, full_date, normalize_record, parse_import, text


_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
STATE_FIELDS = {"version", "generation", "records", "revisions", "imports"}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _empty():
    return {"version": 1, "generation": 0, "records": [], "revisions": [], "imports": []}


def validate_state(value):
    """Pure validation shared with portable backup/import; never creates files."""
    if not isinstance(value, dict) or set(value) != STATE_FIELDS or type(value.get("version")) is not int or value["version"] != 1:
        raise ValueError("经营数据格式或版本不受支持。")
    if type(value.get("generation")) is not int or not 0 <= value["generation"] <= 10**12:
        raise ValueError("经营数据代次无效。")
    def finite(item, depth=0):
        if depth > 20:
            raise ValueError("经营数据嵌套过深。")
        if isinstance(item, dict):
            if any(not isinstance(k, str) for k in item):
                raise ValueError("经营数据键必须为文本。")
            for child in item.values():
                finite(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                finite(child, depth + 1)
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError("经营数据不能含非有限数字。")
        elif item is not None and not isinstance(item, (str, int, float, bool)):
            raise ValueError("经营数据含不支持的值。")
    finite(value)
    for field, maximum in (("records", 100000), ("revisions", 200000), ("imports", 100000)):
        if not isinstance(value.get(field), list) or len(value[field]) > maximum or any(not isinstance(r, dict) for r in value[field]):
            raise ValueError("经营数据列表无效或过量。")
    def record_check(record):
        if not isinstance(record.get("kind"), str) or record["kind"] not in KINDS or not isinstance(record.get("id"), str) or not re.fullmatch(r"[a-f0-9]{64}", record["id"]):
            raise ValueError("经营记录ID或类型无效。")
        if not isinstance(record.get("identity"), str) or record["identity"] != record["id"] or type(record.get("revision")) is not int or record["revision"] < 1:
            raise ValueError("经营记录标识或修订无效。")
        if not isinstance(record.get("product"), str) or not record["product"]:
            raise ValueError("经营记录缺商品ID。")
        if record["kind"] in {"shipments", "aftersales"} and (not isinstance(record.get("businessId"), str) or not record["businessId"]):
            raise ValueError("经营事件缺稳定业务明细ID。")
        if not isinstance(record.get("sourceSystem"), str) or _identity(record) != record["id"]:
            raise ValueError("经营记录业务主键与身份指纹不符。")
        if any(record.get(field) is not None and not isinstance(record[field], str)
               for field in ("sourceSystem", "product", "sku", "specification", "warehouse", "warehouseId", "unit", "baseUnit", "date", "originalDate", "periodStart", "periodEnd")):
            raise ValueError("经营记录标识、日期或单位类型无效。")
        for field in ("date", "originalDate", "statusDate", "periodStart", "periodEnd"):
            if record.get(field) and full_date(record[field]) != record[field]:
                raise ValueError("经营记录日期无效。")
        if record["kind"] in {"shipments", "aftersales"}:
            normalized = normalize_record(record, record["kind"])
            if any(normalized.get(k) != record.get(k) for k in ("quantity", "pack", "normalizedQuantity")):
                raise ValueError("经营记录单位换算与原始数量不符。")
        if record["kind"] == "loss":
            normalized = normalize_record(record, "loss")
            if any(normalized.get(k) != record.get(k) for k in ("registered", "opening", "inbound", "outbound", "physical", "unitPrice", "registeredAmount")):
                raise ValueError("货损记录数量或金额类型无效。")
    ids = set()
    for record in value["records"]:
        record_check(record)
        if record["id"] in ids:
            raise ValueError("经营记录ID重复。")
        ids.add(record["id"])
    for revision in value["revisions"]:
        if not isinstance(revision.get("recordId"), str) or not isinstance(revision.get("record"), dict):
            raise ValueError("经营修订记录无效。")
        record_check(revision["record"])
        if revision["recordId"] != revision["record"]["id"]:
            raise ValueError("经营修订关联无效。")
    scope_fields = {"sourceSystem", "product", "sku", "specification", "warehouseId", "warehouse", "shop"}
    declaration_fields = {"kind", "start", "end", "complete", "evidence", "zeroRecordsConfirmed", "scope", "sourceSha256", "filename"}
    for imported in value["imports"]:
        if not isinstance(imported.get("kind"), str) or imported["kind"] not in KINDS or not isinstance(imported.get("id"), str):
            raise ValueError("经营导入记录类型或标识无效。")
        if any(type(imported.get(field)) is not int or imported[field] < 0 for field in ("saved", "unchanged", "skipped", "issueCount")):
            raise ValueError("经营导入计数无效。")
        coverage = imported.get("coverage")
        if not isinstance(coverage, dict) or set(coverage) != {"observedDates", "start", "end", "declaration"}:
            raise ValueError("经营来源覆盖结构无效。")
        dates = coverage.get("observedDates")
        if not isinstance(dates, list) or len(dates) > 20000 or any(not isinstance(day, str) or full_date(day) != day for day in dates):
            raise ValueError("经营来源观察日期无效。")
        for field in ("start", "end"):
            if coverage[field] is not None and (not isinstance(coverage[field], str) or full_date(coverage[field]) != coverage[field]):
                raise ValueError("经营来源覆盖日期无效。")
        declaration = coverage.get("declaration")
        if declaration is None:
            continue
        if not isinstance(declaration, dict) or set(declaration) != declaration_fields or declaration.get("kind") != imported["kind"]:
            raise ValueError("经营来源完整性声明无效。")
        if type(declaration.get("complete")) is not bool or type(declaration.get("zeroRecordsConfirmed")) is not bool:
            raise ValueError("经营来源完整性标记无效。")
        if any(not isinstance(declaration.get(field), str) for field in ("start", "end", "evidence", "filename")):
            raise ValueError("经营来源声明字段无效。")
        if full_date(declaration["start"]) != declaration["start"] or full_date(declaration["end"]) != declaration["end"] or declaration["start"] > declaration["end"]:
            raise ValueError("经营来源声明期间无效。")
        if declaration["complete"] and not declaration["evidence"]:
            raise ValueError("完整来源声明缺依据。")
        if not isinstance(declaration["scope"], list) or len(declaration["scope"]) > 20000:
            raise ValueError("经营来源实体范围无效。")
        for entity in declaration["scope"]:
            if not isinstance(entity, dict) or set(entity) != scope_fields or any(not isinstance(v, str) for v in entity.values()) or not entity["product"]:
                raise ValueError("经营来源实体须包含有效商品、来源和仓库字段。")
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > 80 * 1024 * 1024:
        raise ValueError("经营数据超过迁移与存储限制。")
    return value


def _semantic(record):
    excluded = {"source", "id", "identity", "revision", "createdAt", "updatedAt", "businessAnchor"}
    return {k: v for k, v in record.items() if k not in excluded}


def _identity(record):
    kind = record["kind"]
    if kind in {"shipments", "aftersales"}:
        values = [kind, record["sourceSystem"], record["businessId"]]
    elif kind == "loss":
        values = [kind, record["sourceSystem"], record["product"], record.get("sku"), record.get("specification"),
                  record.get("warehouseId") or record.get("warehouse"), record.get("periodStart"), record.get("periodEnd"), record.get("unit")]
    else:
        values = [kind, record["sourceSystem"], record.get("layout"), record["product"], record.get("sku"),
                  record.get("specification"), record.get("warehouseId") or record.get("warehouse"), record.get("shop"), record.get("date")]
    return fingerprint(values)


def _coverage_declaration(raw, kind, source, records):
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise ValueError("覆盖说明须为对象。")
    start, end = full_date(raw.get("start")), full_date(raw.get("end"))
    if not start or not end or start > end:
        raise ValueError("覆盖说明须有有效的开始和结束日期。")
    complete = raw.get("complete") is True
    evidence = text(raw.get("evidence") or raw.get("sourceEvidence"))
    if complete and not evidence:
        raise ValueError("完整覆盖须填写来源及范围依据，不能只勾选。")
    if complete and not records and not raw.get("zeroRecordsConfirmed") is True:
        raise ValueError("空记录完整覆盖须显式确认本期间确为0笔。")
    fields = ("sourceSystem", "product", "sku", "specification", "warehouseId", "warehouse", "shop")
    supplied_scope = raw.get("scope")
    if supplied_scope is not None and (not isinstance(supplied_scope, list) or len(supplied_scope) > 20000
                                       or any(not isinstance(entity, dict) for entity in supplied_scope)):
        raise ValueError("覆盖实体须为商品、来源系统和仓库范围列表。")
    entities = supplied_scope if supplied_scope is not None else records
    scope = {}
    for entity in entities:
        normalized = {field: text(entity.get(field)) for field in fields}
        if not normalized["product"]:
            raise ValueError("覆盖实体缺商品ID，不能仅用日期声明完整。")
        scope[fingerprint(normalized)] = normalized
    # Coverage of an export is a user assertion with provenance, not inferred from row dates.
    return {"kind": kind, "start": start, "end": end, "complete": complete, "evidence": evidence,
            "zeroRecordsConfirmed": raw.get("zeroRecordsConfirmed") is True,
            "scope": list(scope.values()),
            "sourceSha256": source.get("sha256") if source else None,
            "filename": source.get("filename") if source else "手工范围声明"}


class OperationsStore:
    def __init__(self, data_dir):
        self.directory = Path(data_dir).expanduser().resolve() / "operations"
        self.path = self.directory / "state.json"
        # Preview and pure reads do not create a database or touch customer files.
        self._tokens = {}
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(str(self.path), threading.RLock())

    def _read(self):
        if not self.path.exists():
            return _empty()
        if self.path.is_symlink() or self.directory.is_symlink():
            raise ValueError("经营数据路径不能是符号链接。")
        try:
            return validate_state(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("经营数据无法读取，请保留文件并恢复备份。") from error

    def _write(self, state):
        validate_state(state)
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink() or self.directory.is_symlink():
            raise ValueError("经营数据路径不能是符号链接。")
        descriptor, temporary = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False, allow_nan=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def preview_records(self, body):
        return self.preview_import(None, body)

    def preview_import(self, entry, body):
        if not isinstance(body, dict):
            raise ValueError("请提供导入参数。")
        parsed = parse_import(entry, body)
        policy = body.get("onConflict", "replace")
        if policy not in {"replace", "skip"}:
            raise ValueError("冲突策略只能是replace或skip。")
        declaration = _coverage_declaration(body.get("coverage"), parsed["kind"], parsed["source"], parsed["records"])
        with self._lock:
            state = self._read()
            existing = {record["identity"]: record for record in state["records"]}
            review, incoming = [], {}
            issues = list(parsed["issues"])
            for record in parsed["records"]:
                record["identity"] = record["id"] = _identity(record)
                key = record["identity"]
                if key in incoming:
                    if _semantic(incoming[key]) != _semantic(record):
                        issues.append({"code": "IN_FILE_CONFLICT", "row": record["source"].get("row"),
                                       "message": "同一业务ID或汇总范围在本文件有不同值，需先明确来源/规格/状态。"})
                        # Neither conflicting version is silently selected.
                        incoming[key]["_blocked"] = True
                    continue
                incoming[key] = record
            counts = dict.fromkeys(("new", "unchanged", "conflict", "blocked"), 0)
            for key, record in incoming.items():
                if record.pop("_blocked", False):
                    counts["blocked"] += 1
                    continue
                old = existing.get(key)
                status = "new" if old is None else "unchanged" if _semantic(old) == _semantic(record) else "conflict"
                counts[status] += 1
                review.append({"status": status, "incoming": record, "existing": deepcopy(old)})
            dates = sorted({r["incoming"].get("date") for r in review if r["incoming"].get("date")})
            result = {"kind": parsed["kind"], "review": review, "reviewCounts": counts, "issues": issues,
                      "records": [r["incoming"] for r in review], "validCount": len(review),
                      "invalidCount": sum(i.get("severity") != "warning" for i in issues),
                      "coverage": {"observedDates": dates, "start": dates[0] if dates else None,
                                   "end": dates[-1] if dates else None, "declaration": declaration},
                      "source": parsed["source"], "onConflict": policy, "generation": state["generation"]}
            result["previewHash"] = result["previewFingerprint"] = fingerprint(result)
            token = uuid.uuid4().hex
            result["token"] = token
            self._tokens = {k: v for k, v in self._tokens.items() if time.monotonic() - v["time"] < 600}
            self._tokens[token] = {"time": time.monotonic(), "preview": deepcopy(result), "stateHash": fingerprint(state)}
            return deepcopy(result)

    def commit_import(self, token, previewHash=None, on_conflict=None, **kwargs):
        if isinstance(token, dict):
            body = token
            previewHash = body.get("previewHash") or body.get("previewFingerprint")
            on_conflict = body.get("onConflict")
            token = body.get("token")
        with self._lock:
            pending = self._tokens.get(text(token))
            if not pending or time.monotonic() - pending["time"] >= 600:
                raise ValueError("预览已过期，请重新预览。")
            preview = pending["preview"]
            if previewHash != preview["previewHash"] or (on_conflict is not None and on_conflict != preview["onConflict"]):
                raise ValueError("预览指纹或冲突策略已变化，请重新预览。")
            state = self._read()
            if fingerprint(state) != pending["stateHash"]:
                raise ValueError("经营数据已变化，请重新预览。")
            source = preview["source"]
            if source:
                try:
                    unchanged = file_sha(source["path"]) == source["sha256"]
                except OSError:
                    unchanged = False
                if not unchanged:
                    raise ValueError("原表已变化或不可读取，请重新预览。")
            if not preview["review"] and not preview["coverage"]["declaration"]:
                raise ValueError("没有可保存记录或明确的零记录覆盖声明。")
            records = {r["identity"]: r for r in state["records"]}
            saved = unchanged = skipped = 0
            for item in preview["review"]:
                if item["status"] == "unchanged":
                    unchanged += 1
                    continue
                if item["status"] == "conflict" and preview["onConflict"] == "skip":
                    skipped += 1
                    continue
                record = deepcopy(item["incoming"])
                old = records.get(record["identity"])
                if old:
                    state["revisions"].append({"recordId": old["id"], "savedAt": _now(), "action": "replace", "record": deepcopy(old)})
                record["revision"] = old["revision"] + 1 if old else 1
                record["createdAt"] = old["createdAt"] if old else _now()
                record["updatedAt"] = _now()
                records[record["identity"]] = record
                saved += 1
            state["records"] = list(records.values())
            state["generation"] += 1
            state["imports"].append({"id": uuid.uuid4().hex, "kind": preview["kind"], "savedAt": _now(),
                                     "source": source, "coverage": preview["coverage"], "saved": saved,
                                     "unchanged": unchanged, "skipped": skipped, "issueCount": len(preview["issues"]),
                                     "previewHash": previewHash})
            self._write(state)
            del self._tokens[text(token)]
            return {"saved": saved, "unchanged": unchanged, "skipped": skipped, "total": len(state["records"]),
                    "generation": state["generation"], "revision": state["generation"], "issues": preview["issues"]}

    def list(self, kind=None, limit=1000, offset=0, start=None, end=None):
        if kind is not None and kind not in KINDS:
            raise ValueError("未知业务记录类型。")
        with self._lock:
            records = self._read()["records"]
        records = [r for r in records if (kind is None or r["kind"] == kind)
                   and (not start or not (r.get("date") or r.get("periodEnd")) or (r.get("date") or r.get("periodEnd")) >= start)
                   and (not end or not (r.get("date") or r.get("periodStart")) or (r.get("date") or r.get("periodStart")) <= end)]
        records.sort(key=lambda r: (r.get("date") or r.get("periodStart") or "", r["product"], r["id"]), reverse=True)
        return {"items": deepcopy(records[max(0, int(offset)):max(0, int(offset)) + min(100000, max(1, int(limit)))]), "total": len(records)}

    def revisions(self, record_id):
        with self._lock:
            items = [r for r in self._read()["revisions"] if r["recordId"] == record_id]
        return {"items": deepcopy(items), "total": len(items)}

    def imports(self):
        with self._lock:
            items = self._read()["imports"]
        return {"items": list(reversed(deepcopy(items))), "total": len(items)}

    @staticmethod
    def _resolve(record, catalog):
        result = deepcopy(record)
        if catalog:
            from .product_identity import resolve_identity
            if isinstance(catalog, dict):
                catalog = catalog.get("items", [])
            identity = resolve_identity(catalog, record["product"], record.get("warehouse", ""), record.get("specification", ""),
                                        record.get("sku", ""), record.get("warehouseId", ""), record.get("sourceSystem", ""))
            result.update({"product": identity["product"], "warehouse": identity["warehouse"], "specification": identity.get("specification", ""),
                           "warehouseId": identity.get("warehouseId", ""), "identityStatus": identity["identityStatus"],
                           "catalogId": identity.get("catalogId"), "catalogRevision": identity.get("catalogRevision")})
        result["standardSku"] = ("mapped:" + result["product"] if result.get("identityStatus") == "mapped" else
                                 fingerprint([result.get("sourceSystem"), result["product"], result.get("sku") or result.get("specification")]))
        return result

    @staticmethod
    def _event_state(record):
        declared = record.get("effectiveState")
        if declared:
            return declared if declared in {"effective", "ignored", "pending"} else "pending"
        status = text(record.get("status")).lower()
        effective = {"shipped", "delivered", "completed", "success", "successful", "已发货", "已签收", "发货成功", "已完成", "退款成功", "退货成功", "成功", "已退款"}
        ignored = {"cancelled", "canceled", "failed", "revoked", "closed", "已取消", "已撤销", "已关闭", "失败", "退款失败", "退货失败"}
        if status in effective:
            return "effective"
        if status in ignored:
            return "ignored"
        return "pending"

    def reconcile(self, start, end, as_of=None, catalog=None, coverage=None, **kwargs):
        start, end = full_date(start), full_date(end)
        as_of = full_date(as_of or kwargs.get("asOf") or end)
        if not start or not end or not as_of or start > end or as_of < end:
            raise ValueError("请指定有效期间和不早于期间结束的观察截止日。")
        with self._lock:
            state = self._read()
        revisions = defaultdict(list)
        for revision in state["revisions"]:
            revisions[revision["recordId"]].append(revision["record"])
        facts = []
        for current in state["records"]:
            if current["kind"] not in {"shipments", "aftersales"}:
                continue
            selected = current
            if current.get("statusDate") and current["statusDate"] > as_of:
                available = [r for r in revisions[current["id"]] if (r.get("statusDate") or r.get("date") or "9999") <= as_of]
                if available:
                    selected = max(available, key=lambda r: (r.get("statusDate") or r.get("date") or "", r["revision"]))
            if not selected.get("statusDate") and selected["revision"] > 1:
                history = sorted([r for r in revisions[current["id"]] if r["revision"] < selected["revision"]] + [selected],
                                 key=lambda r: r["revision"])
                last_dated = max((i for i, r in enumerate(history) if r.get("statusDate")), default=0)
                uncertain = any(not history[i].get("statusDate") and
                                self._event_state(history[i]) != self._event_state(history[i - 1])
                                for i in range(max(1, last_dated + 1), len(history)))
                if uncertain:
                    selected = {**selected, "_uncertainStateRevision": True}
            facts.append(self._resolve(selected, catalog))
        issues, details, by_key = [], [], {}
        contributors = defaultdict(set)
        def issue(record, code, message):
            issues.append({"code": code, "message": message, "recordId": record["id"], "product": record["product"],
                           "businessId": record.get("businessId"), "source": record.get("source"),
                           "date": record.get("date"), "standardSku": record["standardSku"]})
        def bucket(record):
            key = (record["standardSku"], record.get("warehouseId") or record.get("warehouse") or "", record.get("baseUnit") or "")
            contributors[key].add((key[0], key[1], record.get("sourceSystem", ""), record.get("shop", "")))
            if key not in by_key:
                by_key[key] = {"product": record["product"], "sku": record.get("sku", ""), "specification": record.get("specification", ""),
                               "warehouse": record.get("warehouse", ""), "warehouseId": record.get("warehouseId", ""),
                               "sourceSystem": record.get("sourceSystem", ""), "shop": record.get("shop", ""),
                               "unit": record.get("baseUnit", ""), "shipmentQuantity": 0.0, "originalShipmentQuantity": 0.0,
                               "originalDeduction": 0.0, "occurrenceDeduction": 0.0, "pendingCount": 0,
                               "originalPendingCount": 0, "daily": {}}
            return by_key[key]
        def daily(item, day):
            return item["daily"].setdefault(day, {"date": day, "shipmentQuantity": 0.0, "originalShipmentQuantity": 0.0,
                                                  "originalDeduction": 0.0, "occurrenceDeduction": 0.0})
        shipments = []
        for record in facts:
            if record["kind"] != "shipments":
                continue
            day = record.get("date")
            if day and day > as_of:
                continue
            state_name = self._event_state(record)
            if record.get("_uncertainStateRevision"):
                issue(record, "STATE_DATE_MISSING", "发货有效状态已修订但缺状态生效日期，不能将新状态回填历史。")
                bucket(record)["pendingCount"] += 1
                continue
            if record.get("statusDate") and record["statusDate"] > as_of:
                issue(record, "STATE_AFTER_CUTOFF", "当前状态晚于观察截止日且无可用旧版本，不能把当前状态回填历史。")
                bucket(record)["pendingCount"] += 1
                continue
            if state_name == "ignored":
                continue
            item = bucket(record)
            if not day or state_name != "effective" or record.get("normalizedQuantity") is None or record.get("identityStatus") == "ambiguous":
                issue(record, "SHIPMENT_PENDING", "发货日期、有效状态、商品关联或单位换算不足，相关净销量待核对。")
                item["pendingCount"] += 1
                continue
            shipments.append(record)
            if start <= day <= end:
                item["shipmentQuantity"] += record["normalizedQuantity"]
                daily(item, day)["shipmentQuantity"] += record["normalizedQuantity"]
            original_day = record.get("originalDate") or day
            if not record.get("originalDate"):
                issue(record, "ORIGINAL_DATE_MISSING", "未提供独立原销售日期；按原发货日保留观察值，原销售归属结果待核对。")
                item["originalPendingCount"] += 1
            if start <= original_day <= end:
                item["originalShipmentQuantity"] += record["normalizedQuantity"]
                daily(item, original_day)["originalShipmentQuantity"] += record["normalizedQuantity"]
        by_id = {(r["sourceSystem"], r["businessId"]): r for r in shipments}
        deductions = defaultdict(list)
        for record in facts:
            if record["kind"] != "aftersales" or (record.get("date") and record["date"] > as_of):
                continue
            state_name = self._event_state(record)
            if record.get("_uncertainStateRevision"):
                issue(record, "STATE_DATE_MISSING", "售后有效状态已修订但缺状态生效日期，相关扣减待核对。")
                bucket(record)["pendingCount"] += 1
                continue
            if record.get("statusDate") and record["statusDate"] > as_of:
                issue(record, "STATE_AFTER_CUTOFF", "当前售后状态晚于观察截止日且无可用旧版本，不能静默忽略历史事件。")
                bucket(record)["pendingCount"] += 1
                continue
            if state_name == "ignored":
                details.append({"recordId": record["id"], "businessId": record["businessId"], "decision": "ignored", "quantity": 0})
                continue
            candidates = []
            if record.get("shipmentId"):
                match = by_id.get((record["sourceSystem"], record["shipmentId"]))
                candidates = [match] if match else []
            elif record.get("orderId"):
                candidates = [r for r in shipments if r["sourceSystem"] == record["sourceSystem"] and r.get("orderId") == record["orderId"]
                              and r["standardSku"] == record["standardSku"] and (not record.get("lineId") or r.get("lineId") == record["lineId"])]
            kind = text(record.get("type")).lower()
            is_cancel = kind in {"cancel", "cancellation", "取消", "未发货取消"}
            if is_cancel and not candidates:
                details.append({"recordId": record["id"], "businessId": record["businessId"], "decision": "no_shipped_base", "quantity": 0})
                continue
            item = bucket(record)
            reason = None
            if len(candidates) != 1:
                reason = "无唯一原发货关联，不能按商品或比例分摊。"
            elif candidates[0]["standardSku"] != record["standardSku"]:
                reason = "售后商品与原发货商品不一致。"
            elif candidates[0].get("baseUnit") != record.get("baseUnit"):
                reason = "售后与发货统一单位不同。"
            elif record.get("originalDate") and record["originalDate"] != (candidates[0].get("originalDate") or candidates[0]["date"]):
                reason = "售后原销售日期与关联发货日期冲突。"
            elif not record.get("date") or state_name != "effective" or record.get("normalizedQuantity") is None or record.get("identityStatus") == "ambiguous":
                reason = "售后日期、有效状态、数量或单位换算待核对。"
            elif record.get("statusDate") and record["statusDate"] > as_of:
                reason = "售后有效状态晚于观察截止日，不能回填历史。"
            elif kind not in {"return", "refund", "refund_only", "cancel", "cancellation", "退货", "退款", "仅退款", "退货退款", "取消", "未发货取消"}:
                reason = "售后扣减类型未知。"
            if reason:
                issue(record, "AFTERSALE_PENDING", reason)
                item["pendingCount"] += 1
                continue
            shipment = candidates[0]
            # Associate the deduction with the actual source warehouse, never duplicate stock.
            deductions[shipment["id"]].append((record, shipment))
        for shipment_id, pairs in deductions.items():
            shipment = pairs[0][1]
            item = bucket(shipment)
            groups = defaultdict(list)
            for aftersale, _ in pairs:
                groups[aftersale.get("groupId") or aftersale["businessId"]].append(aftersale)
            ungrouped = [a for a, _ in pairs if not a.get("groupId")]
            types = {text(a.get("type")).lower() for a in ungrouped}
            ambiguous_overlap = bool(types & {"return", "退货", "退货退款"} and types & {"refund", "refund_only", "退款", "仅退款"} and len(ungrouped) > 1)
            if ambiguous_overlap:
                issue(ungrouped[0], "OVERLAP_PENDING", "同一发货有退货及退款但缺关联组，不能确认是否重叠；请补groupId。")
                item["pendingCount"] += len(ungrouped)
            resolved_groups = []
            for group_id, rows in groups.items():
                if ambiguous_overlap and not rows[0].get("groupId"):
                    continue
                # Within one explicitly linked case, refund and physical return are one deduction.
                if len({r["normalizedQuantity"] for r in rows}) > 1:
                    issue(rows[0], "GROUP_QUANTITY_PENDING", "同一重叠扣减组数量不一致；请明确部分售后对应关系，不能用最大值隐藏差额。")
                    item["pendingCount"] += 1
                    continue
                quantity = max(r["normalizedQuantity"] for r in rows)
                dates = {r["date"] for r in rows}
                if len(dates) > 1:
                    issue(rows[0], "GROUP_DATE_PENDING", "同一退货退款组有不同发生日期，需明确有效扣减发生日。")
                    item["pendingCount"] += 1
                    continue
                resolved_groups.append((group_id, rows, quantity, next(iter(dates))))
            total = sum(g[2] for g in resolved_groups)
            if total > shipment["normalizedQuantity"] + 1e-9:
                issue(shipment, "OVER_DEDUCTION", "同一发货明细累计有效扣减超过已发数量；保留原值，不能静默截断。")
                item["pendingCount"] += 1
                continue
            for group_id, rows, quantity, occurred in resolved_groups:
                original = shipment.get("originalDate") or shipment["date"]
                if start <= original <= end:
                    item["originalDeduction"] += quantity
                    daily(item, original)["originalDeduction"] += quantity
                if start <= occurred <= end:
                    item["occurrenceDeduction"] += quantity
                    daily(item, occurred)["occurrenceDeduction"] += quantity
                details.append({"groupId": group_id, "recordIds": [r["id"] for r in rows], "businessIds": [r["businessId"] for r in rows],
                                "shipmentId": shipment["businessId"], "product": shipment["product"], "quantity": quantity,
                                "unit": shipment["baseUnit"], "originalDate": original, "occurredDate": occurred, "decision": "deduct_once"})
        declarations = []
        for imported in state["imports"]:
            declaration = (imported.get("coverage") or {}).get("declaration")
            if declaration and imported.get("issueCount", 0) == 0 and imported.get("skipped", 0) == 0:
                declarations.append(declaration)
        # An ad-hoc toggle cannot turn partial facts into a verified complete dataset.
        def entity_key(entity):
            resolved = self._resolve(entity, catalog)
            return (resolved["standardSku"], resolved.get("warehouseId") or resolved.get("warehouse") or "",
                    resolved.get("sourceSystem", ""), resolved.get("shop", ""))
        scoped_declarations = []
        for declaration in declarations:
            entities = {entity_key(entity) for entity in declaration.get("scope", [])}
            scoped_declarations.append((declaration, entities))
        def has_coverage(kind, entity):
            if not entity[1]:
                return False
            spans = sorted((d["start"], d["end"]) for d, entities in scoped_declarations
                           if d["kind"] == kind and d.get("complete") and d.get("evidence") and entity in entities)
            covered_until = None
            from datetime import date, timedelta
            cursor = start
            for left, right in spans:
                if left <= cursor <= right:
                    covered_until = right
                    cursor = (date.fromisoformat(right) + timedelta(days=1)).isoformat()
                    if right >= as_of:
                        break
            return covered_until is not None and covered_until >= as_of
        coverage_by_bucket = {key: {kind: all(has_coverage(kind, entity) for entity in contributors[key])
                                    for kind in ("shipments", "aftersales")} for key in by_key}
        coverage_status = {kind: bool(by_key) and all(v[kind] for v in coverage_by_bucket.values()) for kind in ("shipments", "aftersales")}
        if not all(coverage_status.values()):
            issues.append({"code": "COVERAGE_INCOMPLETE", "message": "缺少与商品、来源系统、店铺、仓库及日期范围相匹配的覆盖声明；当前只展示已观察值。"})
        items = []
        # An unresolved deduction with no warehouse can affect any matching SKU
        # warehouse. Do not mark the known shipment row complete beside it.
        pending_skus = {i.get("standardSku") for i in issues if i.get("standardSku") and i.get("code") != "ORIGINAL_DATE_MISSING"}
        for key, item in by_key.items():
            covered = all(coverage_by_bucket[key].values())
            item["sources"] = [{"sourceSystem": entity[2], "shop": entity[3]} for entity in sorted(contributors[key])]
            original = round(item["originalShipmentQuantity"] - item["originalDeduction"], 10)
            occurred = round(item["shipmentQuantity"] - item["occurrenceDeduction"], 10)
            if key[0] in pending_skus and item["pendingCount"] == 0:
                item["pendingCount"] = 1
            occurrence_complete = covered and item["pendingCount"] == 0
            original_complete = occurrence_complete and item["originalPendingCount"] == 0
            complete = occurrence_complete and original_complete
            item.update({"originalObservedNetQuantity": original, "occurrenceObservedNetQuantity": occurred,
                         "originalNetQuantity": original if original_complete else None, "occurrenceNetQuantity": occurred if occurrence_complete else None,
                         "observedNetQuantity": occurred, "netQuantity": occurred if occurrence_complete else None, "complete": complete,
                         "originalComplete": original_complete, "occurrenceComplete": occurrence_complete,
                         "coverageStatus": coverage_by_bucket[key],
                         "originalDateBasis": "原销售日" if not item["originalPendingCount"] else "原发货日观察，销售日期待核对",
                         "status": "complete" if complete else "pending", "deductionQuantity": item["occurrenceDeduction"]})
            days = list(item["daily"].values())
            for day in days:
                day["originalObservedNetQuantity"] = day["originalShipmentQuantity"] - day["originalDeduction"]
                day["occurrenceObservedNetQuantity"] = day["shipmentQuantity"] - day["occurrenceDeduction"]
            item["daily"] = sorted(days, key=lambda d: d["date"])
            items.append(item)
        # Never add quantities across products or incompatible units into a pretend total.
        result = {"start": start, "end": end, "asOf": as_of, "items": items, "details": details, "issues": issues,
                  "coverage": {"status": coverage_status, "declarations": declarations, "requested": coverage},
                  "complete": bool(items) and all(item["complete"] for item in items),
                  "summary": {"productWarehouseCount": len(items), "pendingCount": sum(i["pendingCount"] for i in items),
                              "issueCount": len(issues)}, "generation": state["generation"],
                  "views": {"original": "原销售归属", "occurrence": "售后发生"}, "basis": "发货/售后事件；业务净销量不代表实物库存变化"}
        return result

    def loss_report(self, start=None, end=None, catalog=None):
        start, end = full_date(start), full_date(end)
        items, issues = [], []
        for record in self.list("loss", limit=100000)["items"]:
            if start and (record.get("periodEnd") or "") < start:
                continue
            if end and (record.get("periodStart") or "9999") > end:
                continue
            record = self._resolve(record, catalog)
            missing = [f for f in ("opening", "inbound", "outbound", "physical") if record.get(f) is None]
            if not record.get("unit"):
                missing.append("unit")
            if not record.get("periodStart") or not record.get("periodEnd"):
                missing.append("period")
            if not record.get("warehouseId") and not record.get("warehouse"):
                missing.append("warehouse")
            if record.get("identityStatus") == "ambiguous":
                missing.append("identity")
            rebuilt = None if missing else round(record["opening"] + record["inbound"] - record["outbound"] - record["physical"], 10)
            difference = round(rebuilt - record["registered"], 10) if rebuilt is not None and record.get("registered") is not None else None
            rebuilt_amount = round(rebuilt * record["unitPrice"], 2) if rebuilt is not None and record.get("unitPrice") is not None else None
            amount_difference = round(rebuilt_amount - record["registeredAmount"], 2) if rebuilt_amount is not None and record.get("registeredAmount") is not None else None
            quantity_status = "uncomputed" if rebuilt is None else "registered_missing" if difference is None else "consistent" if difference == 0 else "difference"
            amount_status = "uncomputed" if amount_difference is None else "consistent" if amount_difference == 0 else "difference"
            discrepancy = quantity_status == "difference" or amount_status == "difference"
            record.update({"rebuilt": rebuilt, "difference": difference, "rebuiltAmount": rebuilt_amount,
                           "amountDifference": amount_difference,
                           "quantityStatus": quantity_status, "amountStatus": amount_status,
                           "missing": missing, "status": "uncomputed" if missing else
                           "confirmed_difference" if discrepancy and record.get("confirmationStatus") == "confirmed" else
                           "awaiting_confirmation" if discrepancy else "consistent" if difference == 0 else "registered_missing",
                           "basis": "公式盘差，不自动认定货损"})
            if missing:
                issues.append({"recordId": record["id"], "message": "缺少：" + "、".join(missing)})
            items.append(record)
        return {"items": items, "total": len(items), "issues": issues}

    def export_workbook(self, report=None, catalog=None):
        with self._lock:
            return self._export_workbook(report, catalog)

    def _export_workbook(self, report=None, catalog=None):
        # Treat an incoming report solely as scope selection. Numbers and
        # generation always come from the current persisted facts.
        if report is not None:
            if not isinstance(report, dict) or not report.get("start") or not report.get("end"):
                raise ValueError("导出净销量须指定统计期间。")
            report = self.reconcile(report["start"], report["end"], as_of=report.get("asOf"), catalog=catalog)
        workbook = openpyxl.Workbook()
        workbook.remove(workbook.active)
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter

        def add(name, rows, columns=None):
            sheet = workbook.create_sheet(name)
            keys = list(columns or dict.fromkeys(key for row in rows for key in row)) or ["说明"]
            sheet.append(keys)
            for row in rows:
                sheet.append([row.get(key) for key in keys])
                for cell in sheet[sheet.max_row]:
                    if isinstance(cell.value, str):
                        cell.data_type = "s"
                        cell.number_format = "@"
                    elif isinstance(cell.value, (int, float)):
                        header = keys[cell.column - 1]
                        cell.number_format = ("0.00%;[Red]-0.00%;0.00%" if "率" in header and "换算率" not in header else
                                              "#,##0.00;[Red]-#,##0.00;0.00" if any(word in header for word in ("金额", "单价", "货款", "报价", "费用", "优惠", "服务费")) else
                                              "#,##0.####;[Red]-#,##0.####;0")
                    cell.alignment = Alignment(vertical="top", wrap_text=True)
                    if cell.row % 2 == 0:
                        cell.fill = PatternFill("solid", fgColor="F2F7FA")
            for cell in sheet[1]:
                cell.fill = PatternFill("solid", fgColor="17354A")
                cell.font = Font(name="等线", size=11, bold=True, color="FFFFFF")
                cell.alignment = Alignment(vertical="center", wrap_text=True)
                cell.border = Border(bottom=Side(style="thin", color="52B4AB"))
            sheet.row_dimensions[1].height = 34
            for index, key in enumerate(keys, 1):
                width = max(12, min(32, len(key) * 2 + 2))
                if any(word in key for word in ("说明", "原因", "依据", "文件", "内容", "原商品信息")):
                    width = 44
                elif "ID" in key or "标识" in key:
                    width = 25
                elif "日期" in key or "期间" in key:
                    width = max(width, 16)
                sheet.column_dimensions[get_column_letter(index)].width = width
            sheet.freeze_panes = "A2"
            sheet.auto_filter.ref = sheet.dimensions
            sheet.sheet_view.showGridLines = False
            sheet.sheet_properties.pageSetUpPr.fitToPage = True
            sheet.page_setup.orientation = "landscape"
            sheet.page_setup.fitToWidth = 1
            sheet.page_setup.fitToHeight = 0
            sheet.print_title_rows = "1:1"
            return sheet

        def yes(value):
            return "是" if value else "否"

        def sources(record):
            source = record.get("source") or {}
            if not isinstance(source, dict):
                return {"来源文件": text(source), "来源工作表": None, "来源行": None, "来源文件SHA-256": None}
            return {"来源文件": source.get("filename"), "来源工作表": source.get("sheet"),
                    "来源行": source.get("row"), "来源文件SHA-256": source.get("sha256")}

        def trace(record):
            return {"修订版本": record.get("revision"), "记录ID": record.get("id"), **sources(record)}

        status_names = {"uncomputed": "未计算", "awaiting_confirmation": "差异待确认", "consistent": "两轨一致",
                        "registered_missing": "缺登记值", "confirmed_difference": "差异已核对", "difference": "存在差异",
                        "pending": "待核对", "confirmed": "已确认", "recheck": "需复核"}
        kind_names = {"summary": "来源汇总", "shipments": "发货事件", "aftersales": "售后事件", "loss": "货损输入"}
        state_names = {"effective": "有效", "ignored": "不计入", "pending": "待核对"}
        type_names = {"return": "退货", "refund": "退款", "refund_only": "仅退款", "cancel": "取消", "cancellation": "取消"}
        decision_names = {"deduct_once": "关联退货与退款仅扣一次", "ignored": "状态无效，不扣减", "no_shipped_base": "未计入已发基数，不重复扣减"}
        start, end = (report.get("start"), report.get("end")) if report else (None, None)
        all_records = self.list(limit=100000)["items"]
        by_id = {record["id"]: record for record in all_records}
        net_columns = ["商品标识", "规格", "仓库", "仓库ID", "数量单位", "原销售期已发基数", "发生期发货量",
                       "原销售期有效扣减", "原销售期净销量", "原销售期已观察净额", "售后发生期有效扣减", "售后发生期净销量",
                       "售后发生期已观察净额", "原销售归属状态", "售后发生状态", "待核对项数", "原销售日期依据", "来源系统", "店铺"]
        if report is not None:
            net_rows = []
            daily_rows = []
            for item in report["items"]:
                net_rows.append({"商品标识": item["product"], "规格": item.get("specification"), "仓库": item.get("warehouse"),
                    "仓库ID": item.get("warehouseId"), "数量单位": item.get("unit"), "原销售期已发基数": item["originalShipmentQuantity"],
                    "发生期发货量": item["shipmentQuantity"], "原销售期有效扣减": item["originalDeduction"],
                    "原销售期净销量": item["originalNetQuantity"], "原销售期已观察净额": item["originalObservedNetQuantity"],
                    "售后发生期有效扣减": item["occurrenceDeduction"], "售后发生期净销量": item["occurrenceNetQuantity"],
                    "售后发生期已观察净额": item["occurrenceObservedNetQuantity"],
                    "原销售归属状态": "可核对" if item["originalComplete"] else "待核对", "售后发生状态": "可核对" if item["occurrenceComplete"] else "待核对",
                    "待核对项数": item["pendingCount"] + item.get("originalPendingCount", 0), "原销售日期依据": item["originalDateBasis"],
                    "来源系统": "；".join(sorted({s.get("sourceSystem") or "未注明" for s in item.get("sources", [])})),
                    "店铺": "；".join(sorted({s.get("shop") or "未注明" for s in item.get("sources", [])}))})
                for day in item["daily"]:
                    daily_rows.append({"业务日期": day["date"], "商品标识": item["product"], "规格": item.get("specification"),
                        "仓库": item.get("warehouse"), "仓库ID": item.get("warehouseId"), "数量单位": item.get("unit"),
                        "原销售日已发基数": day["originalShipmentQuantity"], "当日发货量": day["shipmentQuantity"],
                        "原销售日有效扣减": day["originalDeduction"], "当日售后有效扣减": day["occurrenceDeduction"],
                        "原销售日净销量": day["originalObservedNetQuantity"] if item["originalComplete"] else None,
                        "原销售日已观察净额": day["originalObservedNetQuantity"],
                        "当日售后净销量": day["occurrenceObservedNetQuantity"] if item["occurrenceComplete"] else None,
                        "当日售后已观察净额": day["occurrenceObservedNetQuantity"],
                        "原销售归属状态": "可核对" if item["originalComplete"] else "待核对", "售后发生状态": "可核对" if item["occurrenceComplete"] else "待核对"})
            add("净销量核对", net_rows, net_columns)
            add("每日双视角", daily_rows, ["业务日期", "商品标识", "规格", "仓库", "仓库ID", "数量单位", "原销售日已发基数", "当日发货量",
                "原销售日有效扣减", "当日售后有效扣减", "原销售日净销量", "原销售日已观察净额", "当日售后净销量", "当日售后已观察净额", "原销售归属状态", "售后发生状态"])

        def event_row(record):
            standard = self._resolve(record, catalog)
            return {"业务类型": kind_names[record["kind"]], "业务明细ID": record.get("businessId"), "来源商品ID": record["product"],
                "标准商品标识": standard["product"], "来源SKU": record.get("sku"), "来源规格": record.get("specification"),
                "标准规格": standard.get("specification"), "仓库": standard.get("warehouse"), "仓库ID": standard.get("warehouseId"),
                "业务发生日期": record.get("date"), "原销售日期": record.get("originalDate"), "状态生效日期": record.get("statusDate"),
                "原始状态": record.get("status"), "有效性": state_names[self._event_state(record)],
                "售后类型": type_names.get(record.get("type"), record.get("type")), "原始数量": record.get("quantity"), "原始单位": record.get("unit"),
                "箱规／换算率": record.get("pack"), "换算后数量": record.get("normalizedQuantity"), "换算后单位": record.get("baseUnit"),
                "换算依据版本": record.get("conversionVersion"), "关联发货明细ID": record.get("shipmentId"), "订单ID": record.get("orderId"),
                "订单明细ID": record.get("lineId"), "退货退款关联组ID": record.get("groupId"), "来源系统": record.get("sourceSystem"), "店铺": record.get("shop"), **trace(record)}

        if report is None:
            add("业务事实", [event_row(r) for r in all_records if r["kind"] in {"shipments", "aftersales"}],
                ["业务类型", "业务明细ID", "来源商品ID", "标准商品标识", "业务发生日期", "原始状态", "原始数量", "原始单位", "换算后数量", "换算后单位",
                 "仓库", "仓库ID", "来源系统", "店铺", "来源文件", "来源工作表", "来源行", "修订版本", "记录ID", "来源文件SHA-256"])

        losses = self.loss_report(start, end, catalog=catalog)
        loss_rows = []
        for record in losses["items"]:
            loss_rows.append({"商品标识": record["product"], "规格": record.get("specification"), "仓库": record.get("warehouse"), "仓库ID": record.get("warehouseId"),
                "盘点日期": record.get("date"), "期间开始": record.get("periodStart"), "期间结束": record.get("periodEnd"), "数量单位": record.get("unit"),
                "登记货损数量": record.get("registered"), "期初数量": record.get("opening"), "入库数量": record.get("inbound"), "出库数量": record.get("outbound"),
                "实盘数量": record.get("physical"), "公式盘差": record["rebuilt"], "盘差与登记量差异": record["difference"], "计价单价": record.get("unitPrice"),
                "登记货损金额": record.get("registeredAmount"), "公式盘差金额": record["rebuiltAmount"], "盘差与登记金额差异": record["amountDifference"],
                "数量核对": status_names.get(record["quantityStatus"], record["quantityStatus"]), "金额核对": status_names.get(record["amountStatus"], record["amountStatus"]),
                "处理状态": status_names.get(record.get("confirmationStatus"), "待核对"), "处理说明": record.get("confirmationNote"),
                "结果状态": status_names.get(record["status"], record["status"]), "缺少输入": "、".join(record["missing"]), **trace(record)})
        add("货损双轨", loss_rows, ["商品标识", "规格", "仓库", "仓库ID", "盘点日期", "期间开始", "期间结束", "数量单位", "登记货损数量", "期初数量", "入库数量", "出库数量", "实盘数量",
            "公式盘差", "盘差与登记量差异", "计价单价", "登记货损金额", "公式盘差金额", "盘差与登记金额差异", "数量核对", "金额核对", "处理状态", "处理说明", "结果状态", "缺少输入",
            "来源文件", "来源工作表", "来源行", "修订版本", "记录ID", "来源文件SHA-256"])

        summary_rows = []
        for record in self.list("summary", limit=100000, start=start, end=end)["items"]:
            row = {"来源结构": {"settlement": "商家货款汇总", "warehouse_summary": "仓库业务组汇总", "aftersales_summary": "商品售后率汇总"}.get(record.get("layout"), "来源汇总"),
                   "业务日期": record.get("date"), "日期状态": "已有原表日期" if record.get("date") else "日期／统计期间待核对", "来源商品ID": record["product"],
                   "商品名称": record.get("name"), "规格": record.get("specification"), "仓库": record.get("warehouse"), "仓库ID": record.get("warehouseId"),
                   "店铺": record.get("shop"), "来源系统": record.get("sourceSystem"), "来源原商品信息": record.get("productInfo"), "数量乘报价差额": record.get("quantityPriceDifference")}
            for title, metric in record.get("metrics", {}).items():
                row["原表·" + title] = metric.get("value")
                if metric.get("unit"):
                    row["原表·" + title + "·单位"] = metric["unit"]
            for index, quote in enumerate(record.get("quotes", []), 1):
                row[f"商家报价{index}"] = quote.get("value")
                row[f"报价{index}来源行"] = quote.get("row")
            for check in record.get("ratioChecks", []):
                for field, label in (("reported", "原表值"), ("recalculated", "重算值"), ("difference", "差额")):
                    row[check["metric"] + "·" + label] = check.get(field)
            row.update(trace(record))
            summary_rows.append(row)
        summary_columns = list(dict.fromkeys(key for row in summary_rows for key in row if key not in {"修订版本", "记录ID", "来源文件SHA-256"}))
        add("来源汇总", summary_rows, (summary_columns or ["来源结构", "业务日期", "日期状态", "来源商品ID", "规格", "仓库"]) + ["修订版本", "记录ID", "来源文件SHA-256"])

        def issue_row(issue):
            record = by_id.get(issue.get("recordId"), {})
            source_record = {"source": issue.get("source") or record.get("source")}
            return {"商品标识": issue.get("product") or record.get("product"), "业务明细ID": issue.get("businessId") or record.get("businessId"),
                    "业务日期": issue.get("date") or record.get("date"), "待核对原因": issue.get("message"), **sources(source_record),
                    "提示代码": issue.get("code"), "记录ID": issue.get("recordId")}
        pending = (report.get("issues", []) if report else []) + losses["issues"]
        add("待核对", [issue_row(issue) for issue in pending], ["商品标识", "业务明细ID", "业务日期", "待核对原因", "来源文件", "来源工作表", "来源行", "提示代码", "记录ID", "来源文件SHA-256"])
        if report is not None:
            deductions = []
            for item in report.get("details", []):
                ids = item.get("recordIds") or ([item["recordId"]] if item.get("recordId") else [])
                record = by_id.get(ids[0], {}) if ids else {}
                deductions.append({"商品标识": item.get("product") or record.get("product"), "关联发货明细ID": item.get("shipmentId") or record.get("shipmentId"),
                    "售后业务ID": "；".join(item.get("businessIds") or ([item["businessId"]] if item.get("businessId") else [])),
                    "原销售日期": item.get("originalDate"), "售后发生日期": item.get("occurredDate") or record.get("date"), "有效扣减数量": item.get("quantity"),
                    "数量单位": item.get("unit") or record.get("baseUnit"), "处理结果": decision_names.get(item.get("decision"), item.get("decision")),
                    "退货退款关联组ID": item.get("groupId"), **sources(record), "来源记录ID": "；".join(ids)})
            add("有效扣减", deductions, ["商品标识", "关联发货明细ID", "售后业务ID", "原销售日期", "售后发生日期", "有效扣减数量", "数量单位", "处理结果", "退货退款关联组ID", "来源文件", "来源工作表", "来源行", "来源记录ID", "来源文件SHA-256"])

        event_columns = ["业务类型", "业务明细ID", "来源商品ID", "标准商品标识", "来源SKU", "来源规格", "标准规格", "仓库", "仓库ID", "业务发生日期", "原销售日期", "状态生效日期",
            "原始状态", "有效性", "售后类型", "原始数量", "原始单位", "箱规／换算率", "换算后数量", "换算后单位", "换算依据版本", "关联发货明细ID", "订单ID", "订单明细ID", "退货退款关联组ID",
            "来源系统", "店铺", "来源文件", "来源工作表", "来源行", "修订版本", "记录ID", "来源文件SHA-256"]
        add("发货事实", [event_row(r) for r in all_records if r["kind"] == "shipments"], event_columns)
        add("售后事实", [event_row(r) for r in all_records if r["kind"] == "aftersales"], event_columns)
        if catalog:
            aliases = catalog.get("items", []) if isinstance(catalog, dict) else catalog
            add("商品仓库关联", [{"来源商品ID": r.get("sourceProduct"), "来源SKU": r.get("sourceSku"), "来源规格": r.get("sourceSpecification"),
                "来源系统": r.get("sourceSystem"), "来源仓库": r.get("sourceShop"), "来源仓库ID": r.get("sourceWarehouseId"), "标准商品标识": r.get("product"),
                "商品名称": r.get("name"), "标准规格": r.get("specification"), "标准仓库": r.get("shop"), "标准仓库ID": r.get("warehouseId"),
                "关联版本": r.get("revision"), "关联ID": r.get("catalogId")} for r in aliases],
                ["来源商品ID", "来源SKU", "来源规格", "来源系统", "来源仓库", "来源仓库ID", "标准商品标识", "商品名称", "标准规格", "标准仓库", "标准仓库ID", "关联版本", "关联ID"])

        coverage_rows = []
        for declaration in (report.get("coverage", {}).get("declarations", []) if report else []):
            for entity in declaration.get("scope", []) or [{}]:
                coverage_rows.append({"来源类型": kind_names.get(declaration.get("kind"), declaration.get("kind")), "来源系统": entity.get("sourceSystem"), "商品ID": entity.get("product"),
                    "来源SKU": entity.get("sku"), "规格": entity.get("specification"), "仓库": entity.get("warehouse"), "仓库ID": entity.get("warehouseId"), "店铺": entity.get("shop"),
                    "覆盖开始": declaration.get("start"), "覆盖结束": declaration.get("end"), "完整覆盖声明": yes(declaration.get("complete")),
                    "零记录已确认": yes(declaration.get("zeroRecordsConfirmed")), "覆盖依据": declaration.get("evidence"), "来源文件": declaration.get("filename"), "来源文件SHA-256": declaration.get("sourceSha256")})
        add("来源覆盖", coverage_rows, ["来源类型", "来源系统", "商品ID", "来源SKU", "规格", "仓库", "仓库ID", "店铺", "覆盖开始", "覆盖结束", "完整覆盖声明", "零记录已确认", "覆盖依据", "来源文件", "来源文件SHA-256"])
        scope_rows = [{"项目": "统计开始", "内容": start}, {"项目": "统计结束", "内容": end}, {"项目": "售后观察截止日", "内容": report.get("asOf") if report else None},
            {"项目": "净销量来源与结果是否完整", "内容": "净销量完整来源，可逐项核对" if report and report.get("complete") else "待核对；已观察值不能当作完整净销量"},
            {"项目": "数据代次", "内容": report.get("generation") if report else self._read()["generation"]},
            {"项目": "发货来源覆盖", "内容": "已满足范围要求" if report and report["coverage"]["status"].get("shipments") else "尚未满足范围要求"},
            {"项目": "售后来源覆盖", "内容": "已满足范围要求" if report and report["coverage"]["status"].get("aftersales") else "尚未满足范围要求"},
            {"项目": "原销售归属", "内容": "按原销售日期归属已发数量及截至观察日的有效售后；缺原销售日期时仅保留原发货日观察值"},
            {"项目": "售后发生", "内容": "按发生日期查看当期发货与有效售后冲减，某日净额可以为负；两个视角不相加"},
            {"项目": "缺失与零", "内容": "数字0保留为0；空白表示缺失、不可计算或不适用，详见状态及待核对页"},
            {"项目": "汇总来源", "内容": "原表汇总独立保存，不冒充发货明细或重建净销量；同一合并业务组的多报价不会重复数量"},
            {"项目": "事件明细范围", "内容": "发货与售后事实页包含用于原单关联的历史事件；净销量核对页及每日页按本次期间计算"},
            {"项目": "货损双轨", "内容": "期初＋入库－出库－实盘为公式盘差，与登记值并列；盘差不自动认定货损"},
            {"项目": "货损完整性", "内容": "货损数量、金额及确认状态逐行列示；净销量完整不代表货损输入或业务验收已经完整"},
            {"项目": "单位与金额", "内容": "不同商品与不同单位不混加；售价×数量仅作估算，不等同实际到账或利润"},
            {"项目": "文件保护", "内容": "本次生成新工作簿，未修改源文件；源文件写回另需设置启用及逐次确认"}]
        add("口径说明", scope_rows, ["项目", "内容"])
        return workbook

    def export_bytes(self, report=None):
        stream = io.BytesIO()
        workbook = self.export_workbook(report)
        workbook.save(stream)
        workbook.close()
        return stream.getvalue()


def dispatch(store, method, path, body=None, get_workbook=None, catalog=None, query=None):
    """Small optional HTTP adapter; returns JSON or an openpyxl workbook."""
    body, query = body or {}, query or {}
    tail = path.rstrip("/").split("/")[-1]
    if method == "POST" and tail in {"preview", "import"}:
        entry = get_workbook(body["workbookId"]) if body.get("workbookId") and get_workbook else None
        return store.preview_import(entry, body)
    if method == "POST" and tail == "commit":
        return store.commit_import(body)
    if tail == "reconcile":
        options = body if method == "POST" else query
        return store.reconcile(options.get("start"), options.get("end"), as_of=options.get("asOf"), catalog=catalog, coverage=options.get("coverage"))
    if tail in {"loss", "loss-report"}:
        return store.loss_report(body.get("start") or query.get("start"), body.get("end") or query.get("end"), catalog=catalog)
    if tail == "imports":
        return store.imports()
    if tail == "revisions":
        return store.revisions(body.get("id") or query.get("id"))
    if tail == "export":
        report = store.reconcile(body["start"], body["end"], as_of=body.get("asOf"), catalog=catalog) if body.get("start") else None
        return store.export_workbook(report)
    if method == "GET" and tail in {"operations", "list", "records"}:
        return store.list(kind=query.get("kind"), limit=query.get("limit", 1000), offset=query.get("offset", 0))
    raise ValueError("未知经营核对请求。")
