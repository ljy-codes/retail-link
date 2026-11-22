"""Explicit multi-workbook intake, format baselines, and value-only normalization.

The service never edits an input workbook. It creates a checked, local analysis
copy only after the selected scope has been previewed. A worksheet has one role
or is explicitly excluded, so an unnoticed worksheet cannot look like full data.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid
from collections import defaultdict
from datetime import date, datetime, timezone

import openpyxl
from openpyxl import Workbook
from openpyxl.utils import column_index_from_string, get_column_letter

from . import analyzer


class DataSetError(ValueError):
    pass


class DataSetService:
    MAX_FILES = 8
    MAX_INPUT_BYTES = 80 * 1024 * 1024
    MAX_CELLS = 500000
    MAX_SHEETS = 64
    TOKEN_SECONDS = 600
    ROLES = tuple(analyzer.ROLE_NAMES)
    MOVEMENTS = {"sales", "outbound", "orders", "transit", "loss"}
    FIELDS = ("product", "code", "name", "warehouse", "pack", "plan", "stockLarge", "stockSmall",
              "basePriceLarge", "basePrice", "supplyPrice", "date", "quantity", "amount", "supplyAmount")
    LABELS = {**analyzer.FIELD_LABELS, "name": "商品名称"}
    NUMERIC = set(FIELDS) - {"product", "code", "name", "warehouse", "date"}

    def __init__(self, data_dir, lookup, inspect=None):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.lookup = lookup
        self.inspect = inspect or analyzer.inspect_workbook
        self.sessions_dir = self.data_dir / "import_sessions"
        self.templates_path = self.data_dir / "import_templates.json"
        self._sessions = {}
        self._tokens = {}
        self._lock = threading.RLock()
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _sha(path):
        digest = hashlib.sha256()
        with Path(path).open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _json(value):
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, default=str)

    def _read_templates(self):
        if not self.templates_path.exists():
            return []
        try:
            saved = json.loads(self.templates_path.read_text(encoding="utf-8"))
            if not isinstance(saved, dict) or saved.get("version") != 1 or not isinstance(saved.get("templates"), list):
                raise ValueError()
            return saved["templates"]
        except (OSError, ValueError):
            raise DataSetError("导入格式配置无法读取，请保留原文件并检查本地数据。") from None

    def templates(self):
        with self._lock:
            return [{"id": item["id"], "name": item["name"], "updatedAt": item.get("updatedAt")}
                    for item in self._read_templates()]

    @staticmethod
    def _logical(filename, sheet):
        # Date changes are ordinary daily files, not a new data source.
        clean = lambda value: re.sub(r"\d{4}[-_.年]?\d{2}[-_.月]?\d{2}日?|\d{8}", "{日期}", str(value)).lower().strip()
        return clean(filename) + "::" + clean(sheet)

    @staticmethod
    def _header_value(value, epoch):
        if analyzer._date(value, epoch):
            return "{日期列}"
        return analyzer._norm(value)

    def _structure(self, sheet, header_row, epoch):
        _, width = analyzer._bounds(sheet)
        return {"headerRow": header_row, "columns": width,
                "headers": [[get_column_letter(c), self._header_value(sheet.cell(header_row, c).value, epoch)]
                            for c in range(1, width + 1)],
                "merged": sorted(str(r) for r in sheet.merged_cells.ranges)}

    @staticmethod
    def _fingerprint(structure):
        return hashlib.sha256(json.dumps(structure, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _role_hint(sheet, mapping_by_role):
        name = sheet.title
        for role in DataSetService.ROLES:
            if analyzer._sheet_match([name], role):
                return role
        header_keys = set(analyzer._norm(c.value) for c in analyzer._nonempty(sheet) if c.row <= 3 and isinstance(c.value, str))
        if {"商品总数", "商家报价元"} <= header_keys:
            return "orders"
        if any("库存" in h for h in header_keys):
            return "inventory"
        if any(h in {"销量", "销售数量", "当日销量"} for h in header_keys):
            return "sales"
        return "ignore"

    def _entry(self, workbook_id):
        entry = self.lookup(workbook_id)
        path = Path(entry["path"]).expanduser().resolve()
        if not path.is_file() or path.suffix.lower() not in {".xlsx", ".xlsm"}:
            raise DataSetError("导入源文件已移动或不再可读，请重新选择。")
        actual = self._sha(path)
        expected = entry.get("sha256") or actual
        if actual != expected:
            raise DataSetError("源文件已变化，请重新选择并识别格式。")
        return {"workbookId": str(workbook_id), "filename": entry.get("filename") or path.name,
                "path": str(path), "sha256": actual}

    def _verify_files(self, files):
        for entry in files:
            path = Path(entry["path"])
            if not path.is_file() or self._sha(path) != entry["sha256"]:
                raise DataSetError("源文件在识别后发生变化，请重新选择文件并预览。")

    def _changes(self, sheets, template):
        if not template:
            return []
        saved = template.get("structures", {})
        changes = []
        present = set()
        for sheet in sheets:
            logical = sheet["logicalSource"]
            present.add(logical)
            previous = saved.get(logical)
            kind = "added" if previous is None else "changed" if previous.get("fingerprint") != sheet["fingerprint"] else None
            if kind:
                changes.append({"sourceId": sheet["sourceId"], "kind": kind,
                    "title": "新增工作表" if kind == "added" else "工作表格式变化",
                    "detail": f"{sheet['filename']} / {sheet['sheet']} 的表头、列位置或合并区域与配置“{template['name']}”不同；请核对映射后决定继续。"})
        for logical in sorted(set(saved) - present):
            changes.append({"sourceId": "", "kind": "removed", "title": "原有数据源未出现",
                            "detail": f"配置“{template['name']}”中的 {logical} 未包含在本次文件中，请确认是否有意排除。"})
        return changes

    def stage(self, workbookIds, baselineId=None):
        with self._lock:
            if not isinstance(workbookIds, list) or not 1 <= len(workbookIds) <= self.MAX_FILES:
                raise DataSetError("每次请选择 1—8 份工作簿。")
            files, sheets, warnings, hashes, cells, input_bytes = [], [], [], set(), 0, 0
            configs = []
            observed_dates = set()
            templates = self._read_templates()
            baseline = next((t for t in templates if t["id"] == baselineId), None) if baselineId else None
            if baselineId and baseline is None:
                raise DataSetError("所选格式配置不存在，请重新选择。")
            for workbook_id in workbookIds:
                entry = self._entry(workbook_id)
                if entry["sha256"] in hashes:
                    warnings.append({"code": "DUPLICATE_FILE", "title": "重复文件已排除", "detail": f"{entry['filename']} 内容与已选文件相同，只读取一次。"})
                    continue
                hashes.add(entry["sha256"])
                input_bytes += Path(entry["path"]).stat().st_size
                if input_bytes > self.MAX_INPUT_BYTES:
                    raise DataSetError("所选文件合计超过80MB，请分批导入。")
                files.append(entry)
                inspected = self.inspect(entry["path"])
                configs.append(inspected["config"])
                workbook = openpyxl.load_workbook(entry["path"], data_only=True, read_only=False, keep_links=False)
                try:
                    for index, sheet in enumerate(workbook):
                        rows, cols = analyzer._bounds(sheet)
                        cells += len(analyzer._nonempty(sheet))
                        if cells > self.MAX_CELLS or len(sheets) >= self.MAX_SHEETS or rows > 100000 or cols > 512:
                            raise DataSetError("所选文件内容过大，请分批导入（最多64个工作表、50万个有效单元格）。")
                        hr = analyzer._header_row(sheet)
                        date_col = analyzer._find_col([(c.column, c.value) for c in analyzer._nonempty(sheet) if c.row == hr], "date")
                        if date_col:
                            for row in analyzer._row_numbers(sheet, hr + 1):
                                found = analyzer._date(sheet.cell(row, column_index_from_string(date_col)).value, workbook.epoch)
                                if found:
                                    observed_dates.add(found.isoformat())
                        observed_dates.update(d.isoformat() for _, d in analyzer._date_columns(sheet, hr, workbook.epoch))
                        maps = {role: analyzer._guess_mapping(sheet, role)["columns"] for role in self.ROLES}
                        if "商家报价（元）" in [str(sheet.cell(hr,c).value) for c in range(1,cols+1)]:
                            for role in self.MOVEMENTS:
                                maps[role]["quantity"] = next((get_column_letter(c) for c in range(1,cols+1) if analyzer._norm(sheet.cell(hr,c).value)=="商品总数"), maps[role].get("quantity",""))
                                maps[role]["basePrice"] = next((get_column_letter(c) for c in range(1,cols+1) if analyzer._norm(sheet.cell(hr,c).value)=="商家报价元"), maps[role].get("basePrice",""))
                        role = self._role_hint(sheet, maps) if rows else "ignore"
                        source_id = entry["sha256"][:16] + "-" + str(index)
                        logical = self._logical(entry["filename"],sheet.title)
                        saved = (baseline or {}).get("selections", {}).get(logical)
                        suggested = saved or {"role": role, "headerRow": hr, "columns": maps.get(role, {}),
                                              "unit": "large" if role in {"orders","transit"} else "small"}
                        hr = suggested.get("headerRow", hr)
                        structure = self._structure(sheet, hr, workbook.epoch)
                        sheets.append({"sourceId": source_id, "workbookId": str(workbook_id), "filename": entry["filename"],
                            "sheet": sheet.title, "rows": rows, "columnCount": cols, "headerRow": suggested.get("headerRow",hr),
                            "headers": [{"column":get_column_letter(c),"label":analyzer._text(sheet.cell(hr,c).value)} for c in range(1,cols+1)],
                            "preview": [[analyzer._serial(sheet.cell(r,c).value) for c in range(1,min(cols,12)+1)] for r in range(hr,min(rows,hr+3)+1)],
                            "suggestedRole": suggested.get("role",role), "columns": copy.deepcopy(suggested.get("columns",{})),
                            "mappingByRole":maps, "unit":suggested.get("unit","small"), "snapshotDate":"", "warehouse":suggested.get("warehouse",""),
                            "logicalSource":logical, "structure":structure,"fingerprint":self._fingerprint(structure)})
                finally:
                    workbook.close()
            self._verify_files(files)
            logical_groups = defaultdict(list)
            for info in sheets:
                logical_groups[info["logicalSource"]].append(info)
            for logical, group in logical_groups.items():
                if len(group) <= 1:
                    continue
                warnings.append({"code": "LOGICAL_SOURCE_COLLISION", "title": "每日文件的来源名称相同",
                    "detail": "多份文件去除日期后名称与工作表相同，已分别保留。它们不会共用一项格式配置，请逐项核对映射。"})
                for info in group:
                    info["logicalSource"] = logical + "::" + info["sourceId"]
                    # A single saved source cannot be applied to several daily
                    # files without identifying which one it represented.
                    original_role = self._role_hint_from_info(info)
                    info.update({"suggestedRole": original_role,
                                 "headerRow": info["structure"]["headerRow"],
                                 "columns": copy.deepcopy(info["mappingByRole"].get(original_role, {})),
                                 "unit": "large" if original_role in {"orders", "transit"} else "small",
                                 "warehouse": ""})
            config = copy.deepcopy(configs[0])
            candidates = sorted(observed_dates)
            if candidates:
                config["asOf"] = max(candidates)
                config["period"] = config["asOf"][:7]
            else:
                warnings.append({"code": "ANALYSIS_DATE_DEFAULT", "title": "请核对分析日期",
                                 "detail": "文件内未发现含完整年月日的业务日期，暂沿用默认分析日期；请在确认前按实际业务调整。"})
            config["amountMode"] = "calculated"
            session_id = uuid.uuid4().hex
            staged = {"sessionId":session_id,"files":files,"sheets":sheets,"config":config,
                      "mappingSchema":[{"role":role,"label":analyzer.ROLE_NAMES[role],"fields":[{"key":f,"label":self.LABELS.get(f,f)} for f in [*analyzer.ROLE_FIELDS[role], *(["date"] if role == "inventory" else [])]]} for role in self.ROLES],
                      "templates":self.templates(), "baselineId":baselineId,"formatChanges":self._changes(sheets,baseline),"warnings":warnings,
                      "created":time.monotonic()}
            self._sessions = {key:value for key,value in self._sessions.items() if time.monotonic()-value["created"]<3600}
            if len(self._sessions)>=8:
                self._sessions.pop(next(iter(self._sessions)))
            self._sessions[session_id]=staged
            return copy.deepcopy({k:v for k,v in staged.items() if k!="created"})

    @staticmethod
    def _role_hint_from_info(info):
        for role in DataSetService.ROLES:
            if analyzer._sheet_match([info["sheet"]], role):
                return role
        labels = {analyzer._norm(h["label"]) for h in info["headers"]}
        if {"商品总数", "商家报价元"} <= labels:
            return "orders"
        if any("库存" in label for label in labels):
            return "inventory"
        if labels & {"销量", "销售数量", "当日销量"}:
            return "sales"
        return "ignore"

    @staticmethod
    def _column(value):
        if value in (None, ""):
            return None
        text=str(value).upper().replace("$", "")
        if not re.fullmatch(r"[A-Z]{1,3}",text):
            raise DataSetError("字段列请使用 A、B、AA 等Excel列字母。")
        number=column_index_from_string(text)
        if number>16384:
            raise DataSetError("字段列超过Excel范围。")
        return number

    def _normalize(self, staged, selections, config):
        issues=[]
        issue_keys=set()
        issue_counts=defaultdict(int)
        suppressed=defaultdict(int)
        def issue(code,title,detail,severity="warning",sourceId="",row=None):
            key=(code,sourceId,detail)
            if key in issue_keys:
                return
            if issue_counts[severity] >= 100:
                suppressed[severity] += 1
                return
            issue_keys.add(key)
            issue_counts[severity] += 1
            issues.append({"code":code,"title":title,"detail":detail,"severity":severity,"sourceId":sourceId,"row":row})
        if not isinstance(selections,list):
            raise DataSetError("请逐张工作表指定数据角色或排除。")
        by_id={}
        for selection in selections:
            if not isinstance(selection,dict) or selection.get("sourceId") in by_id:
                raise DataSetError("工作表选择重复或格式无效。")
            by_id[selection.get("sourceId")]=selection
        if set(by_id)!={s["sourceId"] for s in staged["sheets"]}:
            raise DataSetError("请为每张工作表指定数据角色或明确排除，不能遗漏工作表。")
        records=defaultdict(list)
        manifest=[]
        metadata={}
        inspected_sheets=copy.deepcopy(staged["sheets"])
        warehouses=defaultdict(set)
        identities=defaultdict(set)
        ordered=sorted(staged["sheets"],key=lambda s: ({"products":0,"conversion":1,"summary":2,"inventory":3}.get(by_id[s["sourceId"]].get("role"),4)))
        loaded={}
        formula_books={}
        inventory_keys=set()
        inventory_dates=set()
        analysis_date=analyzer._date(config.get("asOf"))
        movement_origins={}
        plans=set()
        try:
            for info in ordered:
                selection=copy.deepcopy(by_id[info["sourceId"]]);role=selection.get("role")
                if role not in (*self.ROLES,"ignore"):
                    raise DataSetError("数据角色无效，请重新选择。")
                entry=next(f for f in staged["files"] if f["sha256"][:16]==info["sourceId"].split("-")[0])
                if entry["path"] not in loaded:
                    loaded[entry["path"]]=openpyxl.load_workbook(entry["path"],data_only=True,read_only=False,keep_links=False)
                    formula_books[entry["path"]]=openpyxl.load_workbook(entry["path"],data_only=False,read_only=False,keep_links=False)
                wb=loaded[entry["path"]];sheet=wb[info["sheet"]];rawsheet=formula_books[entry["path"]][info["sheet"]]
                selection.update({"filename":info["filename"],"sheet":info["sheet"],"logicalSource":info["logicalSource"]})
                manifest.append(selection)
                if role=="ignore":
                    selection["records"]=0
                    continue
                try:hr=int(selection.get("headerRow",1))
                except (ValueError,TypeError):raise DataSetError("表头行必须是1—100的整数。")
                if not 1<=hr<=100:
                    raise DataSetError("表头行必须是1—100的整数。")
                normalized_info = next(item for item in inspected_sheets if item["sourceId"] == info["sourceId"])
                normalized_info["headerRow"] = hr
                normalized_info["structure"] = self._structure(sheet, hr, wb.epoch)
                normalized_info["fingerprint"] = self._fingerprint(normalized_info["structure"])
                columns={key:self._column(value) for key,value in (selection.get("columns") or {}).items() if key in (*self.FIELDS,"amountProduct")}
                if not columns.get("product"):
                    issue("MISSING_PRODUCT","缺少商品标识列",f"{info['filename']} / {sheet.title} 请指定商品标识列。","error",info["sourceId"])
                    continue
                unit=selection.get("unit") or ("large" if role in {"orders","transit"} else "small")
                if unit not in {"large","small"}:
                    raise DataSetError("数量单位应选择大单位或销售小单位。")
                snapshot=analyzer._date(selection.get("snapshotDate")) if selection.get("snapshotDate") else None
                if selection.get("snapshotDate") and not snapshot:
                    issue("INVALID_SNAPSHOT_DATE","业务日期无效","快照业务日期需包含完整年月日。","error",info["sourceId"])
                merges=[r for r in sheet.merged_cells.ranges]
                def value(row,col):
                    if not col:return None
                    v=sheet.cell(row,col).value
                    if v is None:
                        for merged in merges:
                            if merged.min_row<=row<=merged.max_row and merged.min_col<=col<=merged.max_col:
                                v=sheet.cell(merged.min_row,merged.min_col).value;break
                    return v
                def get(row,key):return value(row,columns.get(key))
                date_columns=[]
                for c in analyzer._nonempty(sheet):
                    if c.row==hr:
                        d=analyzer._date(c.value,wb.epoch,snapshot.year if snapshot else None)
                        if d:date_columns.append((c.column,d))
                        elif re.fullmatch(r"\d{1,2}[./月-]\d{1,2}日?",analyzer._text(c.value)):
                            issue("DATE_YEAR_REQUIRED","日期缺少年份",f"{sheet.title} 表头 {c.value} 需指定含年份的业务日期作为年份依据。","error",info["sourceId"])
                date_columns.sort()
                wide=bool(date_columns) and not columns.get("date") and role in self.MOVEMENTS
                if role in self.MOVEMENTS and not wide and not columns.get("date") and snapshot is None:
                    issue("MISSING_DATE","缺少明细日期",f"{sheet.title} 没有日期列或日期表头，请明确填写这份快照的业务日期。","error",info["sourceId"])
                if role == "inventory" and not columns.get("date") and snapshot is None:
                    issue("MISSING_STOCK_DATE", "库存快照日期未指定",
                          f"{sheet.title} 请映射库存日期列，或填写这份库存快照的业务日期，避免将不同日期的库存相加。", "error", info["sourceId"])
                if wide:
                    issue("WIDE_RECALCULATED","日期横表按明细重算",f"{sheet.title} 将日期列展开为明细，整月辅助金额不分摊到各日；按数量与对应单价计算。","warning",info["sourceId"])
                if columns.get("amountProduct"):
                    issue("AUXILIARY_IGNORED","辅助汇总不参与明细合并",f"{sheet.title} 的辅助汇总区不逐行重复计入；本轮合并按明细数量与对应价格重算。","warning",info["sourceId"])
                aux={}
                if columns.get("amountProduct"):
                    for row in analyzer._row_numbers(sheet,hr+1):
                        key=analyzer._text(value(row,columns["amountProduct"]))
                        if key and not analyzer._total_label(key):aux[key]=row
                row_values_seen=set();accepted=0
                source_stock_dates=set()
                for row in analyzer._row_numbers(sheet,hr+1):
                    pid=analyzer._text(get(row,"product"))
                    if not pid or analyzer._total_label(pid):continue
                    record={key:get(row,key) for key in self.FIELDS}
                    record["product"]=pid
                    record["code"]=analyzer._text(record["code"])
                    record["name"]=analyzer._text(record["name"])
                    record["warehouse"]=analyzer._text(record["warehouse"]) or analyzer._text(selection.get("warehouse"))
                    for key in self.NUMERIC:
                        raw=record[key];record[key]=analyzer._number(raw)
                        if raw not in (None,"") and record[key] is None:
                            issue("INVALID_NUMBER","数量或价格无法解析",f"{sheet.title} 第{row}行的{self.LABELS.get(key,key)}不是有效数字，不能静默参与合并。","error",info["sourceId"],row)
                    for key,col in columns.items():
                        if col and rawsheet.cell(row,col).data_type=="f" and value(row,col) is None:
                            issue("MISSING_FORMULA_CACHE","公式没有可读保存值",f"{sheet.title}!{get_column_letter(col)}{row} 没有公式缓存，请由原系统重算保存或改用原始明细。","error",info["sourceId"],row)
                    if record["code"]:
                        identities[(role,pid)].add(record["code"])
                    meta=metadata.setdefault(pid,{"product":pid})
                    if record["pack"] is not None and meta.get("pack") is not None and record["pack"] != meta["pack"]:
                        issue("PACK_CONFLICT", "商品箱规不一致", f"商品 {pid} 在所选来源中对应不同箱规，无法确认单位换算；请调整标识或排除冲突来源。", "error", info["sourceId"], row)
                    meta_fields = ("name", "pack") if role in self.MOVEMENTS else ("name", "pack", "basePrice", "basePriceLarge")
                    for key in meta_fields:
                        if record[key] not in (None,"") and key not in meta:meta[key]=record[key]
                    if record["pack"] is not None and record["pack"]<=0:
                        issue("INVALID_PACK","箱规必须大于零",f"{sheet.title} 第{row}行箱规无效。","error",info["sourceId"],row)
                    pack=record["pack"] or meta.get("pack")
                    record["pack"]=pack
                    if meta.get("basePrice") is None and meta.get("basePriceLarge") is not None and pack:meta["basePrice"]=meta["basePriceLarge"]/pack
                    if meta.get("basePriceLarge") is None and meta.get("basePrice") is not None and pack:meta["basePriceLarge"]=meta["basePrice"]*pack
                    origin={"sourceId":info["sourceId"],"filename":info["filename"],"sheet":sheet.title,"row":row,"role":role,"product":pid}
                    raw_signature=self._json([value(row,c) for c in range(1,info["columnCount"]+1)])
                    if raw_signature in row_values_seen:
                        issue("POSSIBLE_DUPLICATE","存在相同明细行",f"{sheet.title} 第{row}行与之前行内容相同；未自行删行，请确认是否重复登记。","warning",info["sourceId"],row)
                    row_values_seen.add(raw_signature)
                    if role=="summary":
                        if record["plan"] is None:
                            issue("MISSING_PLAN", "计划金额未读取", f"{sheet.title} 第{row}行缺少有效计划金额，请检查计划列映射。", "error", info["sourceId"], row)
                        if pid in plans:
                            issue("DUPLICATE_PLAN","计划商品重复",f"商品 {pid} 的计划出现多次，不能直接相加；请排除重复源或调整商品标识。","error",info["sourceId"],row)
                            continue
                        plans.add(pid)
                    if role=="inventory":
                        if record["stockLarge"] is None and record["stockSmall"] is None:
                            issue("MISSING_STOCK", "库存数量未读取", f"{sheet.title} 第{row}行缺少有效库存数量，请检查大小单位库存列映射。", "error", info["sourceId"], row)
                        stock_date = analyzer._date(get(row, "date"), wb.epoch, snapshot.year if snapshot else None) if columns.get("date") else snapshot
                        if stock_date is None:
                            issue("INVALID_STOCK_DATE", "库存快照日期无效", f"{sheet.title} 第{row}行缺少可确认的库存日期。", "error", info["sourceId"], row)
                        else:
                            record["date"] = stock_date.isoformat()
                            inventory_dates.add(stock_date.isoformat())
                            source_stock_dates.add(stock_date.isoformat())
                            if analysis_date and stock_date > analysis_date:
                                issue("FUTURE_STOCK_DATE", "库存快照晚于分析日",
                                      f"{sheet.title} 的库存快照日期为 {stock_date.isoformat()}，晚于分析截止日 {analysis_date.isoformat()}，不能计入截止日现货；请调整库存来源或分析日期。", "error", info["sourceId"])
                            elif analysis_date and stock_date < analysis_date:
                                issue("OLDER_STOCK_SNAPSHOT", "库存采用较早日期快照",
                                      f"{sheet.title} 采用 {stock_date.isoformat()} 的库存快照，早于分析截止日 {analysis_date.isoformat()}；该库存不代表截止日的实时现货。", "warning", info["sourceId"])
                        if not record["warehouse"]:
                            issue("STOCK_WAREHOUSE_REQUIRED","库存仓库未指定",f"{sheet.title} 第{row}行缺少仓库，请映射仓库列或填写固定仓库。","error",info["sourceId"],row)
                        key=(pid,record["warehouse"])
                        if key in inventory_keys:
                            issue("STOCK_SNAPSHOT_OVERLAP","库存快照范围重叠",f"商品 {pid} 在仓库 {record['warehouse'] or '未指定'} 出现多次，不能把多个日期快照相加。","error",info["sourceId"],row)
                            continue
                        inventory_keys.add(key)
                        warehouses[pid].add(record["warehouse"])
                    if role in self.MOVEMENTS:
                        if wide:
                            for column, _ in date_columns:
                                if rawsheet.cell(row, column).data_type == "f" and value(row, column) is None:
                                    issue("MISSING_FORMULA_CACHE", "公式没有可读保存值", f"{sheet.title}!{get_column_letter(column)}{row} 没有公式缓存，请由原系统重算保存或改用原始明细。", "error", info["sourceId"], row)
                            events=[(d,analyzer._number(value(row,c)),c) for c,d in date_columns if value(row,c) is not None]
                        else:
                            d=analyzer._date(get(row,"date"),wb.epoch,snapshot.year if snapshot else None) if columns.get("date") else snapshot
                            events=[(d,record["quantity"],columns.get("quantity"))]
                        source_price=record["basePrice"]
                        if source_price is None:source_price=meta.get("basePriceLarge" if unit=="large" else "basePrice")
                        supply=record["supplyPrice"]
                        if pid in aux and columns.get("supplyPrice"):
                            supply=analyzer._number(value(aux[pid],columns["supplyPrice"]))
                        for d,qty,column in events:
                            if qty is None:
                                issue("INVALID_QUANTITY","明细数量无效",f"{sheet.title} 第{row}行有无法解析的数量。","error",info["sourceId"],row);continue
                            if d is None:
                                issue("INVALID_DATE","明细日期无效",f"{sheet.title} 第{row}行缺少可确认日期。","error",info["sourceId"],row);continue
                            normalized=copy.deepcopy(record);normalized["date"]=d.isoformat();normalized["quantity"]=qty
                            factor=1.0
                            if role in {"orders","transit"} and unit=="small":factor=1/pack if pack else None
                            elif role not in {"orders","transit"} and unit=="large":factor=pack
                            if factor is None:
                                issue("MISSING_PACK","单位换算缺少箱规",f"商品 {pid} 的{analyzer.ROLE_NAMES[role]}需要箱规才能按所选单位合并。","error",info["sourceId"],row);continue
                            normalized["quantity"]=qty*factor
                            normalized["basePrice"]=source_price/factor if source_price is not None else None
                            normalized["supplyPrice"]=supply/factor if supply is not None else None
                            normalized["amount"]=None;normalized["supplyAmount"]=None
                            normalized["_origin"]={**origin,"cell":get_column_letter(column)+str(row) if column else ""}
                            signature = (role, pid, normalized["date"], normalized["warehouse"], normalized["quantity"],
                                         normalized["basePrice"], normalized["supplyPrice"], normalized["code"])
                            previous = movement_origins.get(signature)
                            if previous and previous["sourceId"] != origin["sourceId"]:
                                issue("POSSIBLE_DUPLICATE_SOURCE", "不同来源存在相同明细",
                                      f"{sheet.title} 第{row}行与 {previous['filename']} / {previous['sheet']} 第{previous['row']}行的日期、商品、仓库、数量及单价相同；两条均保留，请核对是否重复导入。", "warning", info["sourceId"], row)
                            else:
                                movement_origins[signature] = origin
                            records[role].append(normalized);accepted+=1
                    else:
                        record["_origin"]=origin;records[role].append(record);accepted+=1
                selection["records"]=accepted
                if role == "inventory" and source_stock_dates:
                    selection["startDate"] = min(source_stock_dates)
                    selection["endDate"] = max(source_stock_dates)
                    if len(source_stock_dates) == 1:
                        selection["resolvedSnapshotDate"] = next(iter(source_stock_dates))
                if not accepted:
                    issue("EMPTY_SELECTED_SOURCE","选中的数据源没有有效记录",f"{sheet.title} 没有可参与分析的记录，请核对角色、表头行和字段。","warning",info["sourceId"])
            for (role,pid),codes in identities.items():
                if len(codes)>1:
                    issue("AMBIGUOUS_PRODUCT","同一商品标识对应多个编码",f"{analyzer.ROLE_NAMES[role]}中商品 {pid} 对应多个来源编码，请改用可唯一对应规格的编码列作为商品标识。","error")
            if len(inventory_dates) > 1:
                issue("STOCK_DATES_CONFLICT", "库存快照日期不一致",
                      "本次库存包含多个业务日期（" + "、".join(sorted(inventory_dates)) + "），不同仓也不能跨日相加；请只保留同一天的库存。", "error")
            # Collapsing master sheets is safe only for identical facts.
            for role in ("products","conversion"):
                unique={}
                for item in records[role]:
                    pid=item["product"]
                    if pid in unique:
                        previous=unique[pid]
                        if any(item.get(k) not in (None,"") and previous.get(k) not in (None,"") and item.get(k)!=previous.get(k) for k in ("code","pack","basePrice","basePriceLarge")):
                            issue("CONFLICTING_MASTER","商品资料存在冲突",f"商品 {pid} 的{analyzer.ROLE_NAMES[role]}规格或单价不同，请选择明确的数据源。","error")
                    else:unique[pid]=item
                records[role]=list(unique.values())
            if not any(records.values()):
                issue("NO_RECORDS","未选择可分析数据","请至少选择一张含商品数据的工作表并完成映射。","error")
            if sum(selection["role"]!="ignore" for selection in manifest)>1:
                issue("SELECTED_SCOPE","本次按选定范围汇总","各明细按用户指定的仓库和来源合计；合并库存不自动等于平台可售库存池。","info")
            if suppressed:
                issues.append({"code": "ISSUES_TRUNCATED", "title": "同类问题仅显示前100项",
                               "detail": "其余问题数量：" + "、".join(f"{kind} {count}" for kind, count in sorted(suppressed.items())) + "。请先处理已列出的问题，再重新预览。",
                               "severity": "info", "sourceId": "", "row": None})
            return {"records":records,"issues":issues,"manifest":manifest,"metadata":metadata,"inspectedSheets":inspected_sheets,"warehouses":{k:sorted(v) for k,v in warehouses.items()}}
        finally:
            for wb in [*loaded.values(),*formula_books.values()]:wb.close()

    def preview(self, sessionId, selections, config=None):
        with self._lock:
            staged=self._sessions.get(str(sessionId))
            if not staged or time.monotonic()-staged["created"]>3600:
                raise DataSetError("导入会话已过期，请重新选择文件。")
            self._verify_files(staged["files"])
            options={**copy.deepcopy(staged["config"]),**copy.deepcopy(config or {})}
            normalized=self._normalize(staged,selections,options)
            self._verify_files(staged["files"])
            checked_stage = copy.deepcopy(staged)
            checked_stage["sheets"] = normalized.pop("inspectedSheets")
            baseline = next((item for item in self._read_templates() if item["id"] == staged.get("baselineId")), None)
            checked_stage["formatChanges"] = self._changes(checked_stage["sheets"], baseline)
            token=uuid.uuid4().hex
            normalized.update({"staged":checked_stage,"config":options,"created":time.monotonic(),"token":token})
            self._tokens={key:value for key,value in self._tokens.items() if time.monotonic()-value["created"]<self.TOKEN_SECONDS}
            self._tokens[token]=normalized
            selected=sum(s["role"]!="ignore" for s in normalized["manifest"])
            return {"token":token,"canContinue":not any(i["severity"]=="error" for i in normalized["issues"]),
                    "issues":copy.deepcopy(normalized["issues"]),"selectedCount":selected,"excludedCount":len(normalized["manifest"])-selected,
                    "recordCount":sum(len(v) for v in normalized["records"].values()),"selectionManifest":copy.deepcopy(normalized["manifest"]),
                    "formatChanges":copy.deepcopy(checked_stage["formatChanges"]),"warnings":copy.deepcopy(staged["warnings"])}

    def _pending(self,token):
        pending=self._tokens.get(str(token))
        if not pending or time.monotonic()-pending["created"]>self.TOKEN_SECONDS:
            raise DataSetError("导入确认已过期，请重新预览。")
        if any(i["severity"]=="error" for i in pending["issues"]):
            raise DataSetError("存在阻止合并的数据问题，请先调整映射或排除冲突来源。")
        self._verify_files(pending["staged"]["files"])
        return pending

    @staticmethod
    def _set_cell(sheet,row,col,value):
        if value is None:return
        c=sheet.cell(row,col,value)
        if isinstance(value,str):c.data_type="s";c.number_format="@"

    def materialize(self,token):
        with self._lock:
            pending=self._pending(token)
            if pending.get("materialized"):
                self._verify_files(pending["staged"]["files"])
                return copy.deepcopy(pending["materialized"])
            workbook=Workbook();workbook.remove(workbook.active)
            mappings={role:{"sheet":"","headerRow":1,"columns":{}} for role in self.ROLES}
            provenance=[]
            for role in self.ROLES:
                records=list(pending["records"].get(role,[]))
                if role == "products":
                    present = {item["product"] for item in records}
                    # Movement rows may carry their own pack size. Preserve it
                    # in the canonical master, where the existing analyzer
                    # resolves order/transit case quantities.
                    for pid, meta in pending["metadata"].items():
                        if pid not in present and meta.get("pack"):
                            records.append({"product": pid, "name": meta.get("name", ""), "pack": meta["pack"],
                                            "_origin": {"filename": "本次已选数据", "sheet": "按已选箱规归一化", "row": 0,
                                                        "sourceId": "", "role": "products", "product": pid}})
                if not records:continue
                sheet=workbook.create_sheet("合并"+analyzer.ROLE_NAMES[role])
                fields=[*self.FIELDS,"来源文件","来源工作表","原始行"]
                for col,field in enumerate(fields,1):self._set_cell(sheet,1,col,self.LABELS.get(field,field))
                for row,item in enumerate(records,2):
                    origin=item["_origin"]
                    for col,field in enumerate(self.FIELDS,1):self._set_cell(sheet,row,col,item.get(field))
                    for col,field in enumerate(("filename","sheet","row"),len(self.FIELDS)+1):self._set_cell(sheet,row,col,origin[field])
                    provenance.append({"canonicalSheet":sheet.title,"canonicalRow":row,**origin})
                mappings[role]={"sheet":sheet.title,"headerRow":1,"columns":{field:get_column_letter(index+1) for index,field in enumerate(self.FIELDS) if field in analyzer.ROLE_FIELDS[role]}}
                sheet.freeze_panes="A2";sheet.auto_filter.ref=sheet.dimensions
            # Preserve every input sheet's saved values, including explicitly
            # excluded sheets, for visible scope and original-cell inspection.
            raw_sheets=[]
            for index,entry in enumerate(pending["staged"]["files"],1):
                source=openpyxl.load_workbook(entry["path"],data_only=True,read_only=False,keep_links=False)
                try:
                    for sheet_index,sheet in enumerate(source,1):
                        name=f"原表{index}-{sheet_index}-{sheet.title}"[:31]
                        target=workbook.create_sheet(name)
                        for c in analyzer._nonempty(sheet):self._set_cell(target,c.row,c.column,analyzer._serial(c.value))
                        for merged in sheet.merged_cells.ranges:target.merge_cells(str(merged))
                        raw_sheets.append({"filename":entry["filename"],"sheet":sheet.title,"previewSheet":name})
                finally:source.close()
            folder=self.sessions_dir/pending["staged"]["sessionId"];folder.mkdir(exist_ok=True)
            output=folder/(str(token)+".xlsx")
            fd,temp_name=tempfile.mkstemp(prefix=".dataset-",suffix=".xlsx",dir=folder);os.close(fd);temp=Path(temp_name)
            try:
                workbook.save(temp);workbook.close()
                with temp.open("rb") as stream:os.fsync(stream.fileno())
                self._verify_files(pending["staged"]["files"])
                os.replace(temp,output)
            finally:temp.unlink(missing_ok=True)
            options=copy.deepcopy(pending["config"]);options["mappings"]=mappings;options["amountMode"]="calculated"
            # Saved amounts in a wide monthly summary cannot be distributed
            # across dated sources. The interface reports this rebuild clearly.
            result={"path":str(output),"config":options,"inputFiles":copy.deepcopy(pending["staged"]["files"]),
                    "selectionManifest":copy.deepcopy(pending["manifest"]),"formatChanges":copy.deepcopy(pending["staged"]["formatChanges"]),
                    "provenance":provenance,"rawSheets":raw_sheets,"warehouseLabelsByProduct":pending["warehouses"],
                    "issues":copy.deepcopy(pending["issues"])}
            pending["materialized"]=result
            pending["records"] = {}
            pending["metadata"] = {}
            return copy.deepcopy(result)

    def accept(self,token,templateName):
        with self._lock:
            pending=self._pending(token)
            if not pending.get("materialized"):
                raise DataSetError("请先成功生成并分析数据，再保存格式配置。")
            name=str(templateName or "").strip()
            if not name or len(name)>80:
                raise DataSetError("格式配置名称需为1—80个字符。")
            templates=self._read_templates()
            existing=next((t for t in templates if t["name"]==name),None)
            if existing:
                raise DataSetError("已有同名格式配置，请使用新名称保存，原配置不会被覆盖。")
            structures={s["logicalSource"]:{"fingerprint":s["fingerprint"],"structure":s["structure"]} for s in pending["staged"]["sheets"]}
            selections={item["logicalSource"]:{k:v for k,v in item.items() if k in {"role","headerRow","columns","unit","warehouse"}} for item in pending["manifest"]}
            saved={"id":uuid.uuid4().hex,"name":name,"updatedAt":datetime.now(timezone.utc).isoformat(),
                   "structures":structures,"selections":selections}
            templates=[t for t in templates if t["id"]!=saved["id"]]+[saved]
            fd,temporary=tempfile.mkstemp(prefix=".import-templates-",suffix=".json",dir=self.data_dir)
            temp=Path(temporary)
            try:
                with os.fdopen(fd,"w",encoding="utf-8") as output:
                    output.write(self._json({"version":1,"templates":templates}));output.flush();os.fsync(output.fileno())
                os.replace(temp,self.templates_path)
            finally:temp.unlink(missing_ok=True)
            return {"id":saved["id"],"name":name,"updatedAt":saved["updatedAt"]}


# Conventional capitalization is also accepted for callers written before the
# intake contract was finalized.
DatasetService = DataSetService
