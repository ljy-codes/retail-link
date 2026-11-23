"""Verified local workbook generation, recoverable replacement and JSONL audit."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import unicodedata
import uuid

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo


class ExportError(ValueError):
    pass


class AlreadyExistsError(ExportError):
    pass


_LOCK_GUARD = threading.Lock()
_LOCKS = {}


def _shared_lock(key):
    with _LOCK_GUARD:
        return _LOCKS.setdefault(str(key), threading.RLock())


def _now():
    return datetime.now(timezone.utc).isoformat()


class ExportService:
    """Generated workbooks only. Original workbooks stay read-only.

    Replacements retain a flushed, hash-verified copy of the old file.
    Explicit restore verifies both hashes and preserves the replaced version.
    All locks and work are request-driven, without polling or background workers.
    """

    VERSION = "2"
    SHEETS = ("看盘总览", "商品明细", "库存与销量", "数据问题", "数据来源")
    BASIS = {"orders_amount": "报单金额", "outbound_amount": "出库金额", "orders_quantity": "报单小单位数量",
             "outbound_quantity": "出库小单位数量", "sales_quantity": "平台销售小单位数量"}
    SENSITIVE_KEY = re.compile(r"token|cookie|password|secret|credential|authorization|api.?key|sourceinfo|path|url", re.I)
    LOG_FIELDS = {"operationId", "sourceOperationId", "snapshotId", "sourceId", "source", "target", "mode", "confirmation",
                  "backupPath", "backupSha256", "sha256", "sha256Before", "sha256After", "bytes", "result", "sheets",
                  "errorCode", "errorClass", "fileCount", "counts", "formatVersion"}

    def __init__(self, data_dir, app_version="0.1.2"):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.app_version = str(app_version)
        self.exports_dir = self.data_dir / "exports"
        self.backups_dir = self.exports_dir / "backups"
        self.operations_dir = self.exports_dir / "operations"
        self.logs_dir = self.data_dir / "logs"
        self.log_path = self.logs_dir / "operations.jsonl"
        self._log_lock = _shared_lock(self.log_path)
        for path in (self.backups_dir, self.operations_dir, self.logs_dir):
            path.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_filename(value, suffix=".xlsx"):
        raw = str(value or "").strip()
        if not raw:
            raw = f"零售通看盘-{datetime.now().strftime('%Y%m%d-%H%M%S')}{suffix}"
        if raw.startswith("~$") or Path(raw).name != raw or raw in {".", ".."} or re.search(r'[\\/:*?"<>|\x00-\x1f]', raw):
            raise ExportError("文件名不能包含目录、系统保留字符或以 ~$ 开头。")
        stem = raw[:-len(suffix)] if raw.lower().endswith(suffix) else raw
        if Path(stem).suffix and not raw.lower().endswith(suffix):
            raise ExportError(f"生成文件必须使用 {suffix} 格式。")
        while len((stem + suffix).encode("utf-8")) > 230:
            stem = stem[:-1]
        if not stem or stem.endswith((".", " ")):
            raise ExportError("请使用有效的文件名。")
        return stem + suffix

    @staticmethod
    def _directory(value):
        if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
            raise ExportError("请选择一个现有导出目录。")
        original = Path(value).expanduser().absolute()
        for path in (original, *original.parents):
            if path.is_symlink():
                raise ExportError("请选择实际目录，不通过符号链接写入。")
        if not original.is_dir():
            raise ExportError("导出目录不存在，请选择一个现有目录。")
        return original.resolve()

    @staticmethod
    def _check_target(path):
        if path.is_symlink():
            raise ExportError("目标为符号链接，不能写入。")
        if path.exists() and not path.is_file():
            raise ExportError("目标不是普通文件，不能写入。")
        if path.with_name("~$" + path.name).exists() or path.with_name("~$" + path.name[2:]).exists():
            raise ExportError("文件可能正在 Excel 中使用，请关闭后再写入。")

    @staticmethod
    def _cell_value(value):
        if value is None or isinstance(value, (str, bool, int)):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ExportError("导出包含无效数值，请重新分析后再生成。")
            return value
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)

    @classmethod
    def _append(cls, sheet, values):
        sheet.append([cls._cell_value(value) for value in values])
        for cell in sheet[sheet.max_row]:
            if isinstance(cell.value, str):
                if len(cell.value) > 32767 or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", cell.value):
                    raise ExportError("导出文本包含不受支持的控制字符或长度过长。")
                cell.data_type = "s"
                cell.number_format = "@"

    @staticmethod
    def _display_units(text):
        return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(text))

    def _style_sheet(self, sheet, widths, table_name):
        line = Side(style="thin", color="D9E1EA")
        sheet.freeze_panes = "A2"
        sheet.sheet_view.showGridLines = False
        sheet.sheet_view.zoomScale = 95
        sheet.row_dimensions[1].height = 30
        for col, width in enumerate(widths, 1):
            sheet.column_dimensions[get_column_letter(col)].width = width
        for row in sheet:
            header = row[0].row == 1
            lines = 1
            for cell in row:
                cell.font = Font(name="Arial", size=11, bold=header, color="FFFFFF" if header else "16202A")
                cell.fill = PatternFill("solid", fgColor="1F4E79" if header else "F7F9FC" if cell.row % 2 else "FFFFFF")
                cell.alignment = Alignment(horizontal="left" if header or not isinstance(cell.value, (int, float)) else "right",
                                           vertical="center", wrap_text=True)
                cell.border = Border(bottom=line)
                if cell.value is not None:
                    width = widths[cell.column - 1] - 2
                    lines = max(lines, sum(max(1, math.ceil(self._display_units(part) / max(1, width))) for part in str(cell.value).splitlines()))
            if not header:
                sheet.row_dimensions[row[0].row].height = min(409, max(27, lines * 16 + 10))
        if sheet.max_row > 1:
            table = Table(displayName=table_name, ref=sheet.dimensions)
            table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=False)
            sheet.add_table(table)
        sheet.auto_filter.ref = sheet.dimensions
        sheet.print_title_rows = "1:1"
        sheet.page_setup.orientation = "landscape"
        sheet.page_margins.left = .3
        sheet.page_margins.right = .3
        sheet.page_margins.top = .4
        sheet.page_margins.bottom = .4

    @staticmethod
    def _text_id(value):
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value) if value is not None else None

    @classmethod
    def _clean_config(cls, value):
        if isinstance(value, dict):
            return {str(key): cls._clean_config(item) for key, item in value.items() if not cls.SENSITIVE_KEY.search(str(key))}
        if isinstance(value, list):
            return [cls._clean_config(item) for item in value]
        return value

    def _workbook(self, result):
        wb = Workbook()
        overview = wb.active
        overview.title = self.SHEETS[0]
        summary = result.get("summary") or {}
        config = result.get("config") or {}
        basis = config.get("planBasis", "orders_amount")
        money = basis.endswith("amount")
        individual = summary.get("assessmentScope") == "product" and summary.get("assessmentVersion") == 1
        unit = "元" if money else "小单位"
        self._append(overview, ["指标", "值", "单位或说明"])
        overview_rows = [
            ("源文件", result.get("filename"), "原文件保持不变"),
            ("分析快照ID", self._text_id(result.get("id")), "按保存的结果生成，不重新计算"),
            ("导出范围", result.get("exportScope", "完整分析快照"), "商品明细可筛选；总览汇总对应完整快照"),
            ("分析生成时间", result.get("generatedAt"), "来源快照时间"),
            ("统计月份", result.get("period"), ""), ("截止日期", result.get("asOf"), "库存使用导入快照"),
            ("应用版本", result.get("appVersion") or self.app_version, ""),
            ("计划口径", self.BASIS.get(basis, str(basis)), "完成额采用当次保存配置"),
            ("考核范围", "逐个商品独立考核" if individual else "历史记录口径", "超额商品不能抵扣其他商品缺口" if individual else "未记录单品考核版本，保留保存时的数值，重新分析可生成新口径记录"),
            ("金额模式", "明细重算" if config.get("amountMode") == "calculated" else "源金额优先", "跨期金额可按有效明细重算，详见数据问题"),
            ("商品数", summary.get("productCount", len(result["products"])), "款"),
            ("月计划", summary.get("plan"), "元" if money else "小单位"),
            ("累计完成", summary.get("completed"), "元" if money else "小单位"),
            ("合计完成比例" if individual else "达成率", summary.get("achievement"), "累计完成 ÷ 月计划；不代表每个商品达标" if individual else "累计完成 ÷ 月计划"),
            ("目标门槛", summary.get("targetRate", config.get("targetRate")), "按当前配置"),
            ("逐款缺口合计" if individual else "距目标差额", summary.get("targetGap"), unit + ("；逐款正差额相加，资料不全时留空" if individual else "；历史保存值")),
            ("现货金额", summary.get("stockAmount"), "元；现货与在途分开"),
            ("在途金额", summary.get("transitAmount"), "元；未计入现货"),
            ("出库金额", summary.get("outboundAmount"), "元；到岸价口径"),
            ("供价金额", summary.get("supplyAmount"), "元"),
            ("折扣比例", summary.get("discountRate"), "供价金额 ÷ 出库到岸金额"),
            ("折价幅度", summary.get("markdownRate"), "1 − 折扣比例"),
            ("货损金额", summary.get("lossAmount"), "元"),
            ("货损率", summary.get("lossRate"), "货损金额 ÷ 出库到岸金额"),
            ("累计销量", summary.get("salesQuantity"), "销售小单位；按当前统计期间"),
            ("数据问题数", summary.get("issueCount", len(result.get("issues") or [])), "条"),
            ("部分汇总指标", "、".join({"plan": "月计划", "completed": "累计完成", "stockAmount": "现货金额",
                "outboundAmount": "出库金额", "supplyAmount": "供价金额", "lossAmount": "货损金额"}.get(key, key)
                for key in summary.get("partialMetrics") or []), "仅包含已提供数据，必要比率可能留空"),
            ("空白值说明", "缺失或无法计算的指标保留空白", "空白不等于真实0"),
        ]
        if individual:
            task_note = "截止日后本月剩余自然日；按全部商品缺口合计计算"
            if summary.get("remainingDays") == 0 and summary.get("belowTargetCount", 0):
                task_note = "本期已到期，仍有未达标商品；不把未完成任务显示为0"
            elif summary.get("unknownTargetCount", 0):
                task_note = "仍有待核对商品；完整每日任务保留空白"
            insertion = next(i for i, row in enumerate(overview_rows) if row[0] == "现货金额")
            overview_rows[insertion:insertion] = [
                ("已达标商品", summary.get("metCount"), "款"),
                ("未达标商品", summary.get("belowTargetCount"), "款"),
                ("待核对商品", summary.get("unknownTargetCount"), "款；缺计划、完成数据或计划非正数"),
                ("已知商品缺口", summary.get("knownTargetGap"), unit + "；只包括资料完整的商品"),
                ("剩余自然日", summary.get("remainingDays"), "天；截止日之后至月末"),
                ("剩余每日需完成", summary.get("dailyRequired"), unit + "；" + task_note),
                ("已知商品每日需完成", summary.get("knownDailyRequired"), unit + "；只包含可评价商品，存在待核对商品时不是完整任务"),
            ]
        for label, value, note in overview_rows:
            self._append(overview, [label, value, note])
            cell = overview.cell(overview.max_row, 2)
            if label in {"达成率", "合计完成比例", "目标门槛", "折扣比例", "折价幅度", "货损率"}:
                cell.number_format = "0.00%"
            elif isinstance(value, (int, float)) and label not in {"商品数", "数据问题数", "已达标商品", "未达标商品", "待核对商品", "剩余自然日"}:
                cell.number_format = "#,##0.00"
        self._style_sheet(overview, [23, 40, 68], "OverviewMetrics")

        fields = [
            ("id", "商品标识", 22), ("name", "商品名称", 26), ("code", "商品编码", 24), ("warehouse", "仓库或渠道", 20),
            ("plan", "月计划", 16), ("completed", "累计完成", 16), ("achievement", "达成率", 14),
            ("targetGap", "距目标差额", 16), ("dailyRequired", "剩余每日需完成", 20),
            ("outboundQuantity", "出库小单位数量", 20), ("outboundAmount", "出库到岸金额", 20),
            ("supplyAmount", "出库供价金额", 20), ("discountRate", "折扣比例", 14), ("markdownRate", "折价幅度", 14),
            ("lossQuantity", "货损数量", 16), ("lossAmount", "货损金额", 16), ("lossRate", "货损率", 14), ("status", "数据状态", 18),
        ]
        if individual:
            fields[7:7] = [("targetAmount", "考核目标完成额" if money else "考核目标数量", 20),
                           ("assessmentStatus", "单品考核", 16), ("assessmentReason", "考核说明", 42)]
            insertion = next(i for i, field in enumerate(fields) if field[0] == "dailyRequired") + 1
            fields[insertion:insertion] = [
                ("remainingDays", "剩余自然日", 16), ("taskStatus", "每日任务说明", 28)]
        inventory_fields = [
            ("id", "商品标识", 22), ("name", "商品名称", 26), ("code", "商品编码", 24), ("warehouse", "仓库或渠道", 20),
            ("pack", "箱规或换算率", 18), ("stockLarge", "现货大单位", 17), ("stockSmall", "现货小单位", 17),
            ("stockAmount", "现货金额", 18), ("transitSmall", "在途小单位", 17), ("transitAmount", "在途金额", 18),
            ("salesQuantity", "累计销量", 17), ("yesterdaySales", "昨日销量", 17), ("averageSales", "日均销量", 17),
            ("coverageDays", "现货可售天数", 18),
        ]
        for name, definitions, table_name in [(self.SHEETS[1], fields, "ProductDetails"), (self.SHEETS[2], inventory_fields, "InventorySales")]:
            sheet = wb.create_sheet(name)
            self._append(sheet, [field[1] for field in definitions])
            for item in result["products"]:
                values = []
                for key, _, _ in definitions:
                    value = item.get(key)
                    if key in {"id", "code"}:
                        value = self._text_id(value)
                    elif key == "status":
                        value = {"normal": "无单品提醒", "warning": "需核对", "error": "数据异常", "info": "口径提示"}.get(value, value)
                    elif key == "assessmentStatus":
                        value = {"met": "已达标", "below": "未达标", "unknown": "待核对"}.get(value, "待核对")
                    elif key == "taskStatus":
                        value = ("待核对资料" if item.get("assessmentStatus") == "unknown" else
                                 "已达标，无新增任务" if item.get("assessmentStatus") == "met" else
                                 "本期已到期，仍未达标" if item.get("remainingDays") == 0 else "按剩余自然日跟进")
                    values.append(value)
                self._append(sheet, values)
                for col, (key, _, _) in enumerate(definitions, 1):
                    cell = sheet.cell(sheet.max_row, col)
                    if key.endswith("Rate") or key == "achievement":
                        cell.number_format = "0.00%"
                    elif isinstance(cell.value, (int, float)):
                        cell.number_format = "#,##0.00" if key.endswith("Amount") or key in {"plan", "completed", "targetGap", "dailyRequired"} and money else "#,##0.##"
            self._style_sheet(sheet, [field[2] for field in definitions], table_name)

        issues = wb.create_sheet(self.SHEETS[3])
        self._append(issues, ["级别", "问题代码", "标题", "详情", "工作表", "单元格", "商品标识"])
        for item in result.get("issues") or []:
            self._append(issues, [{"warning": "需核对", "error": "数据异常", "info": "口径提示"}.get(item.get("severity"), item.get("severity")),
                                  *[item.get(key) for key in ("code", "title", "detail", "sheet", "cell")], self._text_id(item.get("product"))])
        self._style_sheet(issues, [14, 29, 29, 85, 22, 17, 24], "DataIssues")

        sources = wb.create_sheet(self.SHEETS[4])
        self._append(sources, ["类别", "商品标识", "指标或角色", "工作表或来源", "记录数", "开始日期", "结束日期", "说明或配置"])
        for source in result.get("sources") or []:
            self._append(sources, ["工作表", None, source.get("label") or source.get("role"), source.get("sheet"),
                                   source.get("records"), source.get("startDate"), source.get("endDate"), None])
        manifest = result.get("importManifest") or {}
        for source in manifest.get("selectionManifest") or []:
            self._append(sources, ["原始输入", None, source.get("role"),
                str(source.get("filename", "")) + " / " + str(source.get("sheet", "")),
                source.get("recordCount", source.get("records")), None, None,
                "忽略：未计入分析" if source.get("role") == "ignore" else "已确认纳入；" + self._cell_value(self._clean_config(source))])
        for item in result["products"]:
            for key, source in (item.get("sources") or {}).items():
                if not self.SENSITIVE_KEY.search(str(key)):
                    self._append(sources, ["商品来源", self._text_id(item.get("id")), key, source, None, None, None, None])
        for key in ("asOf", "period", "targetRate", "averageDays", "transitDays", "planBasis", "amountMode", "alignSalesYear", "inventoryComparable", "mappings", "overrides"):
            if key in config:
                value = self._clean_config(config[key])
                if isinstance(value, dict):
                    for subkey, subvalue in value.items():
                        self._append(sources, ["分析配置", None, key, subkey, None, None, None, self._cell_value(subvalue)])
                else:
                    self._append(sources, ["分析配置", None, key, None, None, None, None, self._cell_value(value)])
        self._style_sheet(sources, [16, 24, 28, 58, 14, 18, 18, 85], "DataSources")
        wb.properties.title = "零售通电商看盘"
        wb.properties.creator = "零售通"
        wb.properties.description = "静态分析结果；来源工作簿未修改；缺失指标留空。"
        return wb

    @staticmethod
    def _sha256(path):
        digest = hashlib.sha256()
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _sync_file(path):
        with Path(path).open("rb") as stream:
            os.fsync(stream.fileno())

    @staticmethod
    def _sync_directory(path):
        descriptor = None
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @classmethod
    def _expected(cls, wb):
        return {sheet.title: {"rows": sheet.max_row, "cols": sheet.max_column,
                              "values": [[cell.value for cell in row] for row in sheet]} for sheet in wb}

    def _verify_workbook(self, path, expected=None):
        def same_value(actual, wanted):
            if actual in (None, "") and wanted in (None, ""):
                return True
            if isinstance(actual, (int, float)) and not isinstance(actual, bool) and isinstance(wanted, (int, float)) and not isinstance(wanted, bool):
                return math.isclose(actual, wanted, rel_tol=1e-12, abs_tol=1e-9)
            return type(actual) == type(wanted) and actual == wanted

        try:
            with Path(path).open("rb") as stream:
                wb = load_workbook(stream, read_only=True, data_only=False)
                try:
                    if tuple(wb.sheetnames) != self.SHEETS:
                        raise ExportError("导出校验失败：工作表不完整。")
                    for sheet in wb:
                        values = [[cell.value for cell in row] for row in sheet]
                        if expected is not None:
                            want = expected[sheet.title]
                            if sheet.max_row != want["rows"] or sheet.max_column != want["cols"] or not all(same_value(actual, wanted) for row, expected_row in zip(values, want["values"]) for actual, wanted in zip(row, expected_row)):
                                raise ExportError("导出校验失败：数量或单元格内容不一致。")
                        if any(cell.data_type == "f" for row in sheet for cell in row):
                            raise ExportError("导出校验失败：静态结果不应包含公式。")
                finally:
                    wb.close()
        except ExportError:
            raise
        except Exception as exc:
            raise ExportError("导出文件无法重新读取，已保留原文件。") from exc
        return True

    def log_operation(self, operation, payload=None):
        """Append metadata only; never log result rows, values, tokens or credentials."""
        details = {key: value for key, value in (payload or {}).items() if key in self.LOG_FIELDS}
        entry = {"operation": str(operation), "time": _now(), "appVersion": self.app_version, **details}
        serialized = json.dumps(entry, ensure_ascii=False, allow_nan=False) + "\n"
        with self._log_lock:
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
        return entry

    _log = log_operation

    def _metadata(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ExportError("操作记录标识无效。")
        return self.operations_dir / (identifier + ".json")

    def _save_metadata(self, info):
        output = self._metadata(info["operationId"])
        temp = output.with_name("." + output.name + ".tmp")
        try:
            with temp.open("w", encoding="utf-8") as stream:
                json.dump(info, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, output)
            self._sync_directory(output.parent)
        finally:
            temp.unlink(missing_ok=True)

    def _backup(self, path, identifier, prefix="before"):
        digest = self._sha256(path)
        output = self.backups_dir / f"{identifier}-{prefix}-{digest[:12]}{path.suffix}"
        created = False
        try:
            with path.open("rb") as source, output.open("xb") as destination:
                created = True
                shutil.copyfileobj(source, destination, length=1024 * 1024)
                destination.flush()
                os.fsync(destination.fileno())
            if self._sha256(output) != digest:
                raise ExportError("备份校验失败，原文件未替换。")
        except Exception:
            if created:
                output.unlink(missing_ok=True)
            raise
        self._sync_directory(output.parent)
        return output, digest

    def create_xlsx(self, result, target_dir, filename=None, overwrite=False):
        operation_id = uuid.uuid4().hex
        output = temp = backup = None
        target_lock = None
        before = None
        published = False
        try:
            if not isinstance(result, dict) or not result.get("products") or not isinstance(result["products"], list):
                raise ExportError("没有可导出的看盘结果。")
            if not all(isinstance(item, dict) for item in result["products"]):
                raise ExportError("看盘结果格式无效，请重新生成。")
            directory = self._directory(target_dir)
            output = directory / self._safe_filename(filename)
            source_path = (result.get("sourceInfo") or {}).get("path")
            if source_path and (Path(source_path).expanduser().resolve() == output.resolve() or
                                output.exists() and Path(source_path).is_file() and os.path.samefile(source_path, output)):
                raise ExportError("不能把导出结果写到原始工作簿，请选择新文件名。")
            # Hold one lock level through the exception/recovery path as well.
            target_lock = _shared_lock(output)
            target_lock.acquire()
            with _shared_lock(output):
                self._check_target(output)
                exists = output.exists()
                if exists and not overwrite:
                    raise AlreadyExistsError("已有同名文件，请另存为，或明确确认备份后覆盖。")
                if exists:
                    backup, before = self._backup(output, operation_id)
                self.log_operation("export", {"operationId": operation_id, "snapshotId": str(result.get("id", "")),
                                   "target": str(output), "result": "started", "mode": "overwrite" if exists else "new"})
                descriptor, name = tempfile.mkstemp(prefix=".retail-export-", suffix=".xlsx", dir=directory)
                os.close(descriptor)
                temp = Path(name)
                wb = self._workbook(result)
                expected = self._expected(wb)
                try:
                    wb.save(temp)
                finally:
                    wb.close()
                self._sync_file(temp)
                self._verify_workbook(temp, expected)
                digest = self._sha256(temp)
                self._check_target(output)
                if exists:
                    if not output.exists() or self._sha256(output) != before:
                        raise ExportError("生成期间目标文件已变化，已停止覆盖，请重新确认。")
                    os.replace(temp, output)
                else:
                    try:
                        os.link(temp, output)
                    except FileExistsError as exc:
                        raise AlreadyExistsError("生成期间出现同名文件，已保留，请使用其他文件名。") from exc
                    temp.unlink()
                published = True
                self._sync_directory(directory)
                self._verify_workbook(output, expected)
                if self._sha256(output) != digest:
                    raise ExportError("写入后文件发生变化，校验失败。")
                info = {"operationId": operation_id, "path": str(output), "backupPath": str(backup) if backup else None,
                        "backupSha256": before, "sha256": digest, "bytes": output.stat().st_size,
                        "createdAt": _now(), "sheets": list(self.SHEETS), "restored": False}
                self._save_metadata(info)
                self.log_operation("export", {"operationId": operation_id, "snapshotId": str(result.get("id", "")),
                    "target": str(output), "backupPath": info["backupPath"], "backupSha256": before, "sha256": digest,
                    "sha256Before": before, "sha256After": digest, "bytes": info["bytes"], "sheets": info["sheets"], "result": "success"})
                return info
        except Exception as exc:
            recovered = False
            if published and output is not None:
                with _shared_lock(output):
                    try:
                        if backup is not None and before is not None:
                            self._restore_backup(backup, output, before)
                        else:
                            output.unlink(missing_ok=True)
                            self._sync_directory(output.parent)
                        recovered = True
                    except Exception:
                        pass
            try:
                self.log_operation("export", {"operationId": operation_id, "target": str(output) if output else None,
                    "backupPath": str(backup) if backup else None, "result": "rolled_back" if recovered else "failed",
                    "errorClass": type(exc).__name__, "errorCode": "WRITE_RECOVERED" if recovered else "EXPORT_FAILED"})
            except OSError:
                pass
            if recovered:
                raise ExportError("生成或校验失败，已恢复覆盖前文件；未产生有效新结果。") from exc
            if published:
                raise ExportError("写后校验失败且自动恢复未完成，请保留备份并检查目标目录。") from exc
            raise
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
            if target_lock is not None:
                target_lock.release()

    def _restore_backup(self, backup, target, expected_sha):
        if backup.is_symlink() or not backup.is_file() or self._sha256(backup) != expected_sha:
            raise ExportError("备份不存在或完整性校验失败，未执行恢复。")
        descriptor, name = tempfile.mkstemp(prefix=".retail-restore-", suffix=target.suffix, dir=target.parent)
        os.close(descriptor)
        temporary = Path(name)
        try:
            shutil.copyfile(backup, temporary)
            self._sync_file(temporary)
            if self._sha256(temporary) != expected_sha:
                raise ExportError("恢复文件校验失败，目标未改动。")
            os.replace(temporary, target)
            self._sync_directory(target.parent)
            if self._sha256(target) != expected_sha:
                raise ExportError("恢复后校验失败，请保留备份。")
        finally:
            temporary.unlink(missing_ok=True)

    def rollback(self, operation_id):
        try:
            return self._rollback(operation_id)
        except Exception as exc:
            self.log_operation("rollback", {"operationId": uuid.uuid4().hex,
                "sourceOperationId": str(operation_id), "result": "failed", "errorClass": type(exc).__name__})
            raise

    def _rollback(self, operation_id):
        metadata = self._metadata(operation_id)
        try:
            info = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ExportError("找不到该次生成的恢复记录。") from exc
        target = Path(info["path"])
        with _shared_lock(target):
            info = json.loads(metadata.read_text(encoding="utf-8"))
            if info.get("restored"):
                raise ExportError("该次操作已恢复，不能重复执行。")
            if not info.get("backupPath"):
                raise ExportError("本次是新建文件，没有可恢复的覆盖前版本。")
            self._check_target(target)
            if not target.is_file() or self._sha256(target) != info["sha256"]:
                raise ExportError("目标文件已被修改或移动，不能覆盖恢复，请先核对。")
            backup = Path(info["backupPath"])
            if backup.is_symlink() or not backup.is_file() or self._sha256(backup) != info.get("backupSha256"):
                raise ExportError("覆盖前备份校验失败，未执行恢复。")
            reverse_id = uuid.uuid4().hex
            current, current_sha = self._backup(target, reverse_id, "before-restore")
            try:
                self._restore_backup(backup, target, info["backupSha256"])
                reverse = {"operationId": reverse_id, "path": str(target), "backupPath": str(current),
                           "backupSha256": current_sha, "sha256": info["backupSha256"], "bytes": target.stat().st_size,
                           "createdAt": _now(), "sheets": info.get("sheets", []), "restored": False}
                self._save_metadata(reverse)
                info["restored"] = True
                self._save_metadata(info)
                self.log_operation("rollback", {"operationId": reverse_id, "sourceOperationId": operation_id,
                    "target": str(target), "sha256Before": current_sha, "sha256After": reverse["sha256"],
                    "backupPath": str(current), "backupSha256": current_sha, "result": "rolled_back"})
                return {**reverse, "result": "rolled_back", "sourceOperationId": operation_id, "restoredFrom": str(backup)}
            except Exception as exc:
                self._restore_backup(current, target, current_sha)
                self.log_operation("rollback", {"operationId": reverse_id, "sourceOperationId": operation_id,
                    "target": str(target), "result": "failed", "errorClass": type(exc).__name__})
                raise ExportError("恢复未完成，已保留恢复前版本。") from exc

    def list_logs(self, operation_id=None, limit=200):
        try:
            limit = max(1, min(int(limit), 1000))
        except (TypeError, ValueError) as exc:
            raise ExportError("日志数量应为整数。") from exc
        with self._log_lock:
            if not self.log_path.is_file():
                return []
            rows = deque(maxlen=limit)
            with self.log_path.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if operation_id is None or item.get("operationId") == operation_id or item.get("sourceOperationId") == operation_id:
                        rows.append(item)
            return list(rows)

    def export_logs(self, output_path):
        if not output_path:
            raise ExportError("请选择日志保存文件。")
        output = Path(output_path).expanduser().absolute()
        directory = self._directory(output.parent)
        output = directory / self._safe_filename(output.name, ".jsonl")
        if output.exists() or output.is_symlink():
            raise AlreadyExistsError("日志目标已存在，请使用其他文件名。")
        with self._log_lock:
            descriptor, name = tempfile.mkstemp(prefix=".retail-log-", suffix=".jsonl", dir=directory)
            temporary = Path(name)
            try:
                with os.fdopen(descriptor, "wb") as destination:
                    if self.log_path.is_file():
                        with self.log_path.open("rb") as source:
                            shutil.copyfileobj(source, destination)
                    destination.flush()
                    os.fsync(destination.fileno())
                os.link(temporary, output)
                self._sync_directory(directory)
            finally:
                temporary.unlink(missing_ok=True)
        return {"path": str(output), "bytes": output.stat().st_size, "sha256": self._sha256(output)}
