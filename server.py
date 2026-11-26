#!/usr/bin/env python3
"""Local application host for the operations console. No cloud services required."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import sys
import secrets
sys.dont_write_bytecode = True
import signal
import tempfile
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse
from urllib.parse import quote
import webbrowser
import zipfile

APP_DIR = Path(__file__).resolve().parent
# The Windows embeddable distribution uses a ._pth file, which omits the
# script directory from sys.path. Keep local backend imports available there.
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
PROJECT_DIR = APP_DIR
WEB_DIR = APP_DIR / "web"
DATA_DIR = PROJECT_DIR / "本地数据"
ORIGINAL_DIR = PROJECT_DIR / "examples"
SAMPLE = ORIGINAL_DIR / "样例.xlsx"
EXPORT_DIR = None
DESKTOP_MODE = False


def platform_name():
    return "mac" if sys.platform == "darwin" else "windows" if sys.platform == "win32" else "linux"


def application_metadata():
    """The packaged app supplies its own platform release metadata."""
    metadata = {"version": "0.2.1", "platform": platform_name(), "featureLevel": "desktop-4"}
    source = APP_DIR / "版本.json"
    if source.is_file():
        saved = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(saved, dict):
            raise ValueError("应用版本信息格式无效。")
        for key in metadata:
            if key in saved:
                if not isinstance(saved[key], str) or not saved[key] or len(saved[key]) > 80:
                    raise ValueError("应用版本信息格式无效。")
                metadata[key] = saved[key]
    return metadata


APP_METADATA = application_metadata()
VERSION = APP_METADATA["version"]
PLATFORM = APP_METADATA["platform"]
FEATURE_LEVEL = APP_METADATA["featureLevel"]
MAX_UPLOAD = 30 * 1024 * 1024
MAX_BODY = (MAX_UPLOAD * 4 // 3) + 1024 * 1024
WORKBOOKS: dict[str, dict] = {}
WORKBOOK_LOCK = threading.RLock()
HISTORY_STORES: dict[Path, object] = {}
HISTORY_LOCK = threading.RLock()
EXPORT_SERVICES: dict[Path, object] = {}
EXPORT_LOCK = threading.RLock()
IO_LOCK = threading.RLock()
EXPORT_PREVIEWS = {}
MIGRATION_SERVICES = {}
DATASET_SERVICES = {}
OPERATIONS_STORES = {}
WRITEBACK_SERVICES = {}


def product_catalog():
    from backend.daily_records import DailyStore
    from backend.daily_import import list_catalog
    return list_catalog(DailyStore(DATA_DIR))["items"]


def operations_store():
    from backend.operations import OperationsStore
    directory = DATA_DIR.expanduser().resolve()
    if directory not in OPERATIONS_STORES:
        OPERATIONS_STORES[directory] = OperationsStore(directory)
    return OPERATIONS_STORES[directory]


def writeback_service():
    from backend.source_writeback import SourceWriteback
    directory = DATA_DIR.expanduser().resolve()
    if directory not in WRITEBACK_SERVICES:
        WRITEBACK_SERVICES[directory] = SourceWriteback(directory, [resource_directory(), directory / "writeback"])
    return WRITEBACK_SERVICES[directory]


def export_directory():
    return (EXPORT_DIR if EXPORT_DIR is not None else PROJECT_DIR / "生成文件").expanduser().resolve()


def desktop_directories(home=None, platform=None, environ=None):
    """Writable user locations remain independent of read-only app resources."""
    user_home = Path(home) if home is not None else Path.home()
    host_platform = platform or platform_name()
    environment = os.environ if environ is None else environ
    if host_platform == "mac":
        data = user_home / "Library" / "Application Support" / "零售通"
    elif host_platform == "windows":
        data = Path(environment.get("LOCALAPPDATA") or user_home / "AppData" / "Local") / "零售通"
    else:
        data = user_home / ".local" / "share" / "零售通"
    return {"data": data, "original": data / "原始资料",
            "export": user_home / "Documents" / "零售通" / "生成文件"}


def resource_directory():
    if DESKTOP_MODE:
        bundle = next((parent for parent in (APP_DIR, *APP_DIR.parents) if parent.suffix.lower() == ".app"), None)
        if bundle is not None:
            return bundle.resolve()
    return APP_DIR.resolve()


def validate_export_target(path, snapshot=None):
    from backend.export_service import ExportService
    if not path:
        raise ValueError("请指定生成文件的完整路径。")
    target = Path(path).expanduser()
    if target.is_symlink():
        raise ValueError("目标不能是符号链接。")
    target = target.resolve()
    protected_app = target.is_relative_to(resource_directory())
    # A flattened browser checkout keeps default exports beside server.py.
    # A desktop bundle has no writable exception inside its resources.
    if not DESKTOP_MODE and target.is_relative_to(export_directory()):
        protected_app = False
    if target.is_relative_to(ORIGINAL_DIR.resolve()) or target.is_relative_to(DATA_DIR.resolve()) or protected_app:
        raise ValueError("请选择独立的生成文件目录，避免覆盖原始资料或应用数据。")
    with WORKBOOK_LOCK:
        sources = list(WORKBOOKS.values())
    ExportService.validate_source_target(target, snapshot, *sources)
    return target


def configure_runtime(args):
    global DATA_DIR, ORIGINAL_DIR, SAMPLE, EXPORT_DIR, DESKTOP_MODE
    DESKTOP_MODE = bool(args.desktop)
    defaults = desktop_directories() if DESKTOP_MODE else {
        "data": PROJECT_DIR / "本地数据", "original": PROJECT_DIR / "原始资料", "export": PROJECT_DIR / "生成文件"}
    DATA_DIR = (args.data_dir or defaults["data"]).expanduser().resolve()
    ORIGINAL_DIR = (args.original_dir or (DATA_DIR / "原始资料" if DESKTOP_MODE else defaults["original"])).expanduser().resolve()
    EXPORT_DIR = (args.export_dir or defaults["export"]).expanduser().resolve()
    SAMPLE = ORIGINAL_DIR / "样例.xlsx"
    if DESKTOP_MODE:
        protected = resource_directory()
        locations = [DATA_DIR, ORIGINAL_DIR, EXPORT_DIR]
        if args.ready_file:
            locations.append(args.ready_file.expanduser().resolve())
        if any(path.is_relative_to(protected) for path in locations):
            raise ValueError("桌面应用的运行数据、导出和就绪文件必须位于应用包之外。")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ORIGINAL_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)


def write_ready_file(path, payload):
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".retail-ready-", suffix=".json", dir=path.parent)
    pending = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def remove_ready_file(path, payload):
    if path is None or payload is None:
        return
    try:
        # Another app instance may have replaced the same ready path.
        if json.loads(path.read_text(encoding="utf-8")) == payload:
            path.unlink()
    except (OSError, ValueError):
        pass


def watch_parent_stdin(server):
    """The native host owns the pipe's write end; EOF means its process ended."""
    def wait_for_eof():
        try:
            while sys.stdin.buffer.read(1):
                pass
        except (OSError, ValueError):
            pass
        # shutdown() waits for the serving loop and must run off its thread.
        server.shutdown()
    watcher = threading.Thread(target=wait_for_eof, name="retail-parent-pipe", daemon=True)
    watcher.start()


def dataset_service():
    from backend.dataset_service import DataSetService
    directory = DATA_DIR.expanduser().resolve()
    if directory not in DATASET_SERVICES:
        DATASET_SERVICES[directory] = DataSetService(directory, get_workbook)
    return DATASET_SERVICES[directory]


def migration_service():
    from backend.migration_service import MigrationService
    directory = DATA_DIR.expanduser().resolve()
    if directory not in MIGRATION_SERVICES:
        MIGRATION_SERVICES[directory] = MigrationService(directory, audit=export_service().log_operation, app_version=VERSION)
    return MIGRATION_SERVICES[directory]


def preview_export(body):
    from backend.export_service import ExportService
    snapshot = history_store().get(body.get("snapshotId", ""))
    selected = body.get("productIds")
    if selected is not None:
        if not isinstance(selected, list) or not selected:
            raise ValueError("当前筛选没有可导出的商品。")
        selected = set(map(str, selected))
        snapshot["products"] = [p for p in snapshot["products"] if str(p["id"]) in selected]
        if not snapshot["products"]:
            raise ValueError("当前筛选没有可导出的商品。")
        # Retain whole-snapshot overview explicitly; detail is the selected scope.
        snapshot["exportScope"] = "所选商品明细；总览保留完整分析快照汇总"
    else:
        snapshot["exportScope"] = "完整分析快照"
    directory = Path(str(body.get("targetDir", ""))).expanduser().resolve()
    if not directory.is_dir():
        raise ValueError("目标目录不存在，请选择一个现有目录。")
    target = directory / ExportService._safe_filename(body.get("filename"))
    # Generated outputs cannot overwrite any known input or managed runtime file.
    target = validate_export_target(target, snapshot)
    target_state = ExportService.target_state(target)
    token = secrets.token_urlsafe(24)
    now = time.monotonic()
    for key, value in list(EXPORT_PREVIEWS.items()):
        if now - value["created"] > 600:
            EXPORT_PREVIEWS.pop(key, None)
    EXPORT_PREVIEWS[token] = {"created": now, "result": snapshot, "target": target, "targetState": target_state}
    return {"token": token, "path": str(target), "exists": target_state["identity"] is not None,
            "productCount": len(snapshot["products"]), "scope": snapshot["exportScope"],
            "sheets": list(ExportService.SHEETS), "snapshotId": snapshot["id"]}


def engine():
    from backend import analyzer
    return analyzer


def history_store():
    """Reuse a store/lock per persistent data directory across HTTP threads."""
    from backend.history_store import HistoryStore
    directory = DATA_DIR.expanduser().resolve()
    with HISTORY_LOCK:
        if directory not in HISTORY_STORES:
            HISTORY_STORES[directory] = HistoryStore(directory)
        return HISTORY_STORES[directory]


def export_service():
    from backend.export_service import ExportService
    directory = DATA_DIR.expanduser().resolve()
    with EXPORT_LOCK:
        if directory not in EXPORT_SERVICES:
            EXPORT_SERVICES[directory] = ExportService(directory, VERSION)
        return EXPORT_SERVICES[directory]


def history_detail(identifier, deleted=False):
    """Read the stored snapshot; registering its source never recalculates it."""
    result = history_store().get(identifier, deleted=deleted)
    response = {"result": result, "inspection": None, "sourceAvailable": False,
                "sourceMessage": "此记录未保存原始文件位置；历史结果仍可查看，可重新导入文件继续分析。"}
    source = result.get("sourceInfo")
    if not isinstance(source, dict) or not source.get("path") or not source.get("sha256"):
        return response
    path = Path(source["path"]).expanduser()
    try:
        manifest = result.get("importManifest") or {}
        for original in manifest.get("inputFiles") or []:
            original_path = Path(original["path"])
            if not original_path.is_file() or hashlib.sha256(original_path.read_bytes()).hexdigest() != original["sha256"]:
                response["sourceMessage"] = "组合输入中的原文件已移动或变化；历史结果仍保留，重新导入并确认范围后可继续分析。"
                return response
        if not path.is_file():
            response["sourceMessage"] = "原始文件已移动或删除；历史结果仍保留，当前快照只读。"
            return response
        if hashlib.sha256(path.read_bytes()).hexdigest() != source["sha256"]:
            response["sourceMessage"] = "原始文件内容已变化；这里保留修改前的历史快照，请重新导入当前文件。"
            return response
        inspection = register_workbook(path, source.get("filename") or result.get("filename"))
        if inspection["sha256"] != source["sha256"]:
            response["sourceMessage"] = "原始文件读取期间发生变化，历史快照保持只读，请重新导入。"
            return response
        if manifest:
            inspection["config"] = result.get("config") or inspection["config"]
            WORKBOOKS[inspection["workbookId"]]["dataset"] = {**manifest, "config": inspection["config"]}
            inspection["dataset"] = manifest
        response.update({"inspection": inspection, "sourceAvailable": True,
                         "sourceMessage": "原始文件可用；当前展示保存时的结果，重新分析会生成一条新记录。"})
    except (ValueError, OSError) as exc:
        response["sourceMessage"] = f"原始文件暂不可用（{exc}）；已保存的历史结果仍可查看。"
    return response


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, default=str).encode("utf-8")


def check_workbook(path: Path, filename=None):
    if Path(filename or path.name).suffix.lower() not in {".xlsx", ".xlsm"}:
        raise ValueError("请选择 .xlsx 或 .xlsm 文件；旧版 .xls 请先另存为 .xlsx。")
    if not path.is_file():
        raise ValueError("文件不存在，请重新选择。")
    if path.stat().st_size > MAX_UPLOAD:
        raise ValueError("文件超过 30 MB，请拆分后再导入。")
    try:
        with zipfile.ZipFile(path) as archive:
            if "xl/workbook.xml" not in archive.namelist():
                raise ValueError("这不是有效的 Excel 工作簿。")
            if sum(info.file_size for info in archive.infolist()) > 200 * 1024 * 1024:
                raise ValueError("工作簿展开后过大，请减少空白格式区域或拆分文件。")
    except zipfile.BadZipFile as exc:
        raise ValueError("无法读取此文件，请确认文件未损坏或加密。") from exc


def register_workbook(path: Path, display_name=None):
    from backend.source_writeback import has_user_symlink
    selected_path = path.expanduser().absolute()
    selected_via_symlink = has_user_symlink(selected_path)
    path = path.expanduser().resolve()
    check_workbook(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    workbook_id = digest[:24]
    inspected = engine().inspect_workbook(path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError("文件在读取期间发生变化，请等待保存完成后重新导入。")
    with WORKBOOK_LOCK:
        WORKBOOKS[workbook_id] = {
            "path": path, "filename": display_name or path.name, "sha256": digest,
            "size": path.stat().st_size, "mtime": path.stat().st_mtime_ns,
            "selectedPath": str(selected_path), "selectedViaSymlink": selected_via_symlink,
        }
    inspected.update({"workbookId": workbook_id, "filename": display_name or path.name,
                      "sourcePath": str(path), "sha256": digest})
    export_service().log_operation("read", {"source": str(path), "sha256": digest, "bytes": path.stat().st_size, "result": "success"})
    return inspected


def get_workbook(workbook_id):
    with WORKBOOK_LOCK:
        entry = WORKBOOKS.get(str(workbook_id))
    if not entry:
        raise ValueError("文件会话已失效，请重新选择 Excel。")
    if not entry["path"].is_file():
        raise ValueError("原文件已移动或删除，请重新选择。")
    for original in entry.get("dataset", {}).get("inputFiles", []):
        path = Path(original["path"])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != original["sha256"]:
            raise ValueError("组合输入中的原文件已变化或移动，请重新导入并确认范围。")
    stat = entry["path"].stat()
    if stat.st_size != entry["size"] or stat.st_mtime_ns != entry["mtime"]:
        if hashlib.sha256(entry["path"].read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError("文件内容已经变化，请重新导入以保证结果与文件一致。")
    return entry


class Handler(BaseHTTPRequestHandler):
    server_version = "OperationsConsole/" + VERSION

    def log_message(self, fmt, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {fmt % args}", flush=True)

    def send_json(self, value, status=200):
        self.response_status = status
        if status >= 400:
            try:
                export_service().log_operation("request", {"errorCode": "HTTP_" + str(status), "sourceId": urlparse(self.path).path, "result": "failed"})
            except Exception:
                # Error logging must never prevent the actual error response.
                pass
        body = json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def origin_allowed(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        source = urlparse(origin)
        if source.hostname in {"127.0.0.1", "localhost", "::1"} and source.port == self.server.server_port:
            return True
        self.send_json({"error": "请从本机工具页面操作。"}, 403)
        return False

    def dispatch_v02(self, method, parsed, body=None):
        route = parsed.path
        if not route.startswith(("/api/operations", "/api/writeback")):
            return False
        body = body or {}
        query = parse_qs(parsed.query)
        if method == "GET" and route == "/api/operations":
            self.send_json(operations_store().list(kind=query.get("kind", [None])[0]))
        elif method == "GET" and route == "/api/operations/revisions":
            self.send_json(operations_store().revisions(query.get("id", [""])[0]))
        elif method == "GET" and route == "/api/writeback/settings":
            self.send_json(writeback_service().settings())
        elif method == "GET" and route == "/api/writeback/history":
            self.send_json(writeback_service().history())
        elif method == "POST" and route == "/api/operations/import":
            store = operations_store()
            if body.get("previewOnly", True):
                entry = get_workbook(body["workbookId"]) if body.get("workbookId") else None
                self.send_json(store.preview_import(entry, body))
            else:
                if body.get("confirmed") is not True:
                    raise ValueError("请确认业务导入预览后再保存。")
                self.send_json(store.commit_import(body))
                export_service().log_operation("operations_import", {"result": "success"})
        elif method == "POST" and route in {"/api/operations/reconcile", "/api/operations/export", "/api/operations/loss"}:
            store = operations_store()
            if route.endswith("/loss"):
                result = store.loss_report(body.get("start"), body.get("end"), catalog=product_catalog())
            else:
                result = store.reconcile(body.get("start"), body.get("end"), as_of=body.get("asOf"),
                                         catalog=product_catalog(), coverage=body.get("coverage"))
            if route.endswith("/export"):
                from io import BytesIO
                stream = BytesIO()
                book = store.export_workbook(result, catalog=product_catalog())
                book.save(stream)
                book.close()
                self.send_json({"filename": "零售通-经营核对.xlsx", "contentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                "dataBase64": base64.b64encode(stream.getvalue()).decode("ascii")})
            else:
                self.send_json(result)
        elif method == "POST" and route == "/api/writeback/settings":
            if body.get("enabled") is True and body.get("confirmed") is not True:
                raise ValueError("请确认了解源写回将修改所选原件。")
            self.send_json(writeback_service().settings(body.get("enabled")))
        elif method == "POST" and route in {"/api/writeback/template", "/api/writeback/describe", "/api/writeback/preview"}:
            entry = get_workbook(body.get("workbookId"))
            service = writeback_service()
            if route.endswith("/template"):
                result = service.register_template(entry, body)
            elif route.endswith("/describe"):
                result = service.describe(entry, body.get("templateId"))
            else:
                result = service.preview(entry, body)
            self.send_json(result)
        elif method == "POST" and route == "/api/writeback/commit":
            result = writeback_service().commit(body.get("token"), body.get("confirmed") is True)
            export_service().log_operation("source_writeback", {"result": "success", "source": result["path"], "operationId": result["id"]})
            self.send_json(result)
        elif method == "POST" and route == "/api/writeback/restore":
            result = writeback_service().restore(body.get("operationId"), body.get("confirmed") is True)
            export_service().log_operation("source_writeback_restore", {"result": "success", "source": result["path"]})
            self.send_json(result)
        else:
            self.send_json({"error": "经营核对或受控写回接口不存在。"}, 404)
        return True

    def dispatch_daily(self, method, parsed, body=None):
        if parsed.path != "/api/daily" and not parsed.path.startswith("/api/daily/"):
            return False
        try:
            from backend.daily_records import handle_daily_request
        except ImportError:
            self.send_json({"error": "每日售价与销量模块尚未就绪。"}, 503)
            return True
        handled = handle_daily_request(handler=self, method=method, path=parsed.path,
                                    query=parse_qs(parsed.query), body=body,
                                    data_dir=DATA_DIR, lookup=get_workbook)
        if handled and method in {"POST", "DELETE"} and getattr(self, "response_status", 500) < 400:
            if not (parsed.path == "/api/daily/import" and (body or {}).get("previewOnly", True)):
                export_service().log_operation("daily_" + ("delete" if method == "DELETE" else "restore" if parsed.path.endswith("/restore") else "save"),
                    {"result": "success", "sourceId": parsed.path})
        return handled

    def do_GET(self):
        with IO_LOCK:
            self._do_GET()

    def _do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/health":
                return self.send_json({"app": "零售通", "version": VERSION,
                                       "desktop": DESKTOP_MODE, "platform": PLATFORM, "featureLevel": FEATURE_LEVEL,
                                       "sampleAvailable": SAMPLE.is_file(),
                                       "defaultDirectory": str(ORIGINAL_DIR),
                                       "defaultExportDirectory": str(export_directory()),
                                       "capabilities": ["excel", "directory", "manual", "mapping", "history", "local_export", "migration", "multi_import", "format_review", "operations", "data_health", "source_writeback"]})
            if parsed.path == "/api/import/templates":
                return self.send_json({"templates": dataset_service().templates()})
            if self.dispatch_daily("GET", parsed):
                return
            if self.dispatch_v02("GET", parsed):
                return
            if parsed.path == "/api/sample":
                if not SAMPLE.is_file():
                    raise ValueError("当前安装包未附带样例文件，请选择本地 Excel 工作簿。")
                return self.send_json(register_workbook(SAMPLE))
            if parsed.path == "/api/preview":
                query = parse_qs(parsed.query)
                entry = get_workbook(query.get("workbookId", [""])[0])
                sheet = query.get("sheet", [""])[0]
                offset = max(0, int(query.get("offset", ["0"])[0]))
                limit = min(100, max(1, int(query.get("limit", ["30"])[0])))
                return self.send_json(engine().preview_workbook(entry["path"], sheet, offset, limit))
            if parsed.path == "/api/history":
                query = parse_qs(parsed.query)
                return self.send_json(history_store().list(
                    offset=query.get("offset", [0])[0], limit=query.get("limit", [50])[0],
                    deleted=query.get("deleted", ["0"])[0] == "1"))
            if parsed.path == "/api/health/report":
                from backend.data_health import build_health_report
                query = parse_qs(parsed.query)
                identifier = query.get("snapshotId", [""])[0]
                if not identifier:
                    raise ValueError("请指定分析快照。")
                result = history_store().get(identifier, deleted=query.get("deleted", ["0"])[0] == "1")
                return self.send_json(build_health_report(result))
            if parsed.path == "/api/preferences":
                return self.send_json({"preferences": migration_service().load_preferences()})
            if parsed.path.startswith("/api/export/download/"):
                operation_id = parsed.path.removeprefix("/api/export/download/")
                entry = next((item for item in export_service().list_logs(operation_id=operation_id, limit=1000)
                              if item.get("operation") == "export" and item.get("result") == "success"), None)
                if not entry:
                    raise ValueError("导出记录不存在，请重新生成。")
                target = Path(entry["target"])
                if not target.is_file() or target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != entry["sha256"]:
                    raise ValueError("导出文件已变化或不存在，请重新生成。")
                payload = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(target.name))
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)
                return
            if parsed.path == "/api/export/logs":
                query = parse_qs(parsed.query)
                return self.send_json({"items": export_service().list_logs(limit=query.get("limit", [200])[0])})
            if parsed.path.startswith("/api/history/"):
                query = parse_qs(parsed.query)
                identifier = parsed.path.removeprefix("/api/history/")
                return self.send_json(history_detail(identifier, deleted=query.get("deleted", ["0"])[0] == "1"))
            if parsed.path.startswith("/api/"):
                return self.send_json({"error": "接口不存在。"}, 404)
            relative = unquote(parsed.path).lstrip("/") or "index.html"
            target = (WEB_DIR / relative).resolve()
            if not target.is_relative_to(WEB_DIR.resolve()) or not target.is_file():
                return self.send_json({"error": "页面不存在。"}, 404)
            body = target.read_bytes()
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type == "application/javascript":
                content_type += "; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
        except (ValueError, FileNotFoundError, KeyError) as exc:
            self.send_json({"error": str(exc)}, 400)
        except BrokenPipeError:
            pass
        except Exception as exc:
            traceback.print_exc()
            self.send_json({"error": f"读取失败：{exc}"}, 500)

    def do_POST(self):
        with IO_LOCK:
            self._do_POST()

    def _do_POST(self):
        try:
            if not self.origin_allowed():
                return
            if "application/json" not in self.headers.get("Content-Type", ""):
                return self.send_json({"error": "请求需使用 JSON 格式。"}, 415)
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0 or length > MAX_BODY:
                return self.send_json({"error": "文件或请求体过大，最大支持 30 MB 文件。"}, 413)
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("请求内容必须为对象。")
            parsed = urlparse(self.path)
            route = parsed.path
            if route == "/api/import/stage":
                return self.send_json(dataset_service().stage(body.get("workbookIds"), baselineId=body.get("templateId")))
            if route == "/api/import/preview":
                return self.send_json(dataset_service().preview(body.get("sessionId"), body.get("selections"), body.get("config")))
            if route == "/api/import/confirm":
                if body.get("confirmed") is not True:
                    raise ValueError("请先预览并确认纳入的数据范围。")
                template_name = str(body.get("templateName") or "").strip()
                if len(template_name) > 80:
                    raise ValueError("格式配置名称不能超过80个字符。")
                if template_name and any(item["name"] == template_name for item in dataset_service().templates()):
                    raise ValueError("已有同名格式模板，请换一个名称保存本次基线。")
                token = body.get("token", "")
                dataset = dataset_service().materialize(token)
                filenames = [item["filename"] for item in dataset["inputFiles"]]
                inspection = register_workbook(Path(dataset["path"]), "、".join(filenames))
                inspection["config"] = dataset["config"]
                inspection["dataset"] = {"inputFiles": filenames, "selectionManifest": dataset["selectionManifest"], "formatChanges": dataset["formatChanges"]}
                entry = WORKBOOKS[inspection["workbookId"]]
                entry["dataset"] = dataset
                entry["templateToken"] = token
                entry["templateName"] = template_name
                export_service().log_operation("import_review", {"files": len(dataset["inputFiles"]),
                    "sheets": len(dataset["selectionManifest"]), "formatChanges": len(dataset["formatChanges"]), "result": "success"})
                return self.send_json({"inspection": inspection})
            if self.dispatch_daily("POST", parsed, body):
                return
            if self.dispatch_v02("POST", parsed, body):
                return
            if route == "/api/export/preview":
                return self.send_json(preview_export(body))
            if route == "/api/export/xlsx":
                from backend.export_service import AlreadyExistsError, ExportService
                token = body.get("token", "")
                pending = EXPORT_PREVIEWS.get(token)
                if not pending or time.monotonic() - pending["created"] > 600:
                    raise ValueError("导出预览已失效，请重新预览后确认。")
                target = validate_export_target(pending["target"], pending["result"])
                current_state = ExportService.target_state(target)
                if current_state != pending["targetState"]:
                    raise ValueError("目标文件在预览后发生变化，请重新预览。")
                exists = current_state["identity"] is not None
                if exists and body.get("overwrite") is not True:
                    return self.send_json({"error": "存在同名文件，请另存或明确确认覆盖。", "code": "ALREADY_EXISTS"}, 409)
                try:
                    result = export_service().create_xlsx(pending["result"], target.parent, target.name,
                        overwrite=exists, expected_target=pending["targetState"])
                except AlreadyExistsError as exc:
                    return self.send_json({"error": str(exc), "code": "ALREADY_EXISTS"}, 409)
                EXPORT_PREVIEWS.pop(token, None)
                return self.send_json(result)
            if route == "/api/export/rollback":
                if body.get("confirmed") is not True:
                    raise ValueError("请确认恢复覆盖前版本。")
                return self.send_json(export_service().rollback(str(body.get("operationId", ""))))
            if route == "/api/export/logs":
                return self.send_json(export_service().export_logs(validate_export_target(body.get("path"))))
            if route == "/api/preferences":
                return self.send_json(migration_service().save_preferences(body.get("preferences")))
            if route == "/api/data/bundle":
                return self.send_json(migration_service().export_bundle(validate_export_target(body.get("path"))))
            if route == "/api/data/preview":
                return self.send_json(migration_service().preview_import(body.get("path")))
            if route == "/api/data/import":
                result = migration_service().import_bundle(body.get("path"), replace=body.get("replace") is True,
                    confirmation_token=body.get("token"))
                HISTORY_STORES.clear()
                DATASET_SERVICES.clear()
                OPERATIONS_STORES.clear()
                writeback_service().settings(False)
                WRITEBACK_SERVICES.clear()
                return self.send_json(result)
            restore_match = re.fullmatch(r"/api/history/([0-9]{1,64})/restore", route)
            if restore_match:
                result = history_store().restore(restore_match.group(1))
                export_service().log_operation("history_restore", {"sourceId": result["id"], "result": "success"})
                return self.send_json(result)
            if route == "/api/inspect":
                filename = Path(str(body.get("filename", ""))).name
                if Path(filename).suffix.lower() not in {".xlsx", ".xlsm"}:
                    raise ValueError("请选择 .xlsx 或 .xlsm 文件。")
                content = body.get("content", "")
                if not isinstance(content, str):
                    raise ValueError("文件内容格式无效。")
                try:
                    payload = base64.b64decode(content, validate=True)
                except Exception as exc:
                    raise ValueError("文件传输不完整，请重试。") from exc
                if len(payload) > MAX_UPLOAD:
                    raise ValueError("文件超过 30 MB。")
                digest = hashlib.sha256(payload).hexdigest()
                safe_name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", filename)[:160]
                destination = ORIGINAL_DIR / "导入记录" / (digest[:16] + "_" + safe_name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists():
                    pending = destination.with_suffix(destination.suffix + ".part")
                    pending.write_bytes(payload)
                    try:
                        check_workbook(pending, filename)
                    except (ValueError, OSError):
                        pending.unlink(missing_ok=True)
                        raise
                    pending.replace(destination)
                return self.send_json(register_workbook(destination, filename))
            if route == "/api/open":
                return self.send_json(register_workbook(Path(str(body.get("path", "")))))
            if route == "/api/directory":
                directory = Path(str(body.get("path", "")) or ORIGINAL_DIR).expanduser().resolve()
                if not directory.is_dir():
                    raise ValueError("目录不存在，请输入本机文件夹的完整路径。")
                files = [{"name": p.name, "path": str(p), "size": p.stat().st_size,
                          "modified": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")}
                         for p in sorted(directory.iterdir(), key=lambda p: p.name.lower())
                         if p.is_file() and p.suffix.lower() in {".xlsx", ".xlsm"} and not p.name.startswith("~$")]
                return self.send_json({"path": str(directory), "files": files})
            if route == "/api/analyze":
                entry = get_workbook(body.get("workbookId"))
                config = body.get("config", {})
                if not isinstance(config, dict):
                    raise ValueError("分析配置格式无效。")
                if entry.get("dataset") and config.get("mappings", {}) != entry["dataset"].get("config", {}).get("mappings", {}):
                    raise ValueError("组合输入的工作表和字段已按预览范围确认；请通过合并导入重新选择范围和映射。")
                config = dict(config)
                config["productCatalog"] = product_catalog()
                result = engine().analyze_workbook(entry["path"], config)
                if not result.get("products"):
                    raise ValueError("没有识别到商品数据。请在页面配置所选文件的工作表、表头行和商品列，再重新分析。")
                report = operations_store().reconcile(result["period"] + "-01", result["asOf"],
                    as_of=config.get("afterSalesAsOf") or result["asOf"], catalog=config["productCatalog"])
                for product in result["products"]:
                    product["netSales"] = [item for item in report["items"] if str(item["product"]) == str(product["id"])]
                result["operationsSnapshot"] = {key: report[key] for key in ("start", "end", "asOf", "generation", "complete", "coverage", "summary", "basis")}
                result["filename"] = entry["filename"]
                result["workbookId"] = str(body.get("workbookId"))
                result["generatedAt"] = datetime.now(timezone.utc).isoformat()
                result["id"] = str(time.time_ns())
                result["appVersion"] = VERSION
                result["sourceInfo"] = {"path": str(entry["path"]), "sha256": entry["sha256"],
                                        "filename": entry["filename"]}
                if entry.get("dataset"):
                    dataset = entry["dataset"]
                    result["importManifest"] = {key: dataset.get(key) for key in ("inputFiles", "selectionManifest", "formatChanges", "provenance", "issues", "warehouseLabelsByProduct")}
                    for warning in dataset.get("issues") or []:
                        result["issues"].append({"id": "import-" + str(len(result["issues"]) + 1),
                            "severity": warning.get("severity", "warning"), "code": warning.get("code", "IMPORT_REVIEW"),
                            "title": warning.get("title", "导入范围提示"), "detail": warning.get("detail", ""),
                            "sheet": warning.get("sheet", ""), "cell": warning.get("cell", ""), "product": warning.get("product", "")})
                    labels = dataset.get("warehouseLabelsByProduct") or {}
                    for product in result["products"]:
                        warehouses = labels.get(product["id"], [])
                        if warehouses and not product.get("warehouseStocks"):
                            product["warehouse"] = "、".join(warehouses)
                        product["issues"] = [issue for issue in result["issues"] if issue.get("product") == product["id"]]
                        own = product["issues"]
                        product["status"] = "error" if any(i["severity"] == "error" for i in own) else "warning" if any(i["severity"] == "warning" for i in own) else "normal"
                    result["summary"]["issueCount"] = len(result["issues"])
                    if entry.get("templateName"):
                        result["importTemplate"] = dataset_service().accept(entry["templateToken"], entry["templateName"])
                        entry["templateName"] = ""
                saved = history_store().save(result)
                export_service().log_operation("analyze", {"source": str(entry["path"]), "snapshotId": saved["id"], "result": "success"})
                return self.send_json(saved)
            self.send_json({"error": "接口不存在。"}, 404)
        except (ValueError, FileNotFoundError, KeyError, PermissionError) as exc:
            self.send_json({"error": str(exc)}, 400)
        except BrokenPipeError:
            pass
        except Exception as exc:
            traceback.print_exc()
            self.send_json({"error": f"分析失败：{exc}"}, 500)

    def do_DELETE(self):
        with IO_LOCK:
            self._do_DELETE()

    def _do_DELETE(self):
        try:
            if not self.origin_allowed():
                return
            parsed = urlparse(self.path)
            if self.dispatch_daily("DELETE", parsed):
                return
            delete_match = re.fullmatch(r"/api/history/([0-9]{1,64})", parsed.path)
            if delete_match:
                result = history_store().delete(delete_match.group(1))
                export_service().log_operation("history_delete", {"sourceId": result["id"], "result": "success"})
                return self.send_json(result)
            self.send_json({"error": "接口不存在。"}, 404)
        except (ValueError, FileNotFoundError, KeyError, PermissionError) as exc:
            self.send_json({"error": str(exc)}, 400)
        except BrokenPipeError:
            pass
        except Exception as exc:
            traceback.print_exc()
            self.send_json({"error": f"删除失败：{exc}"}, 500)


def main(argv=None):
    parser = argparse.ArgumentParser(description="零售通 · 电商看盘")
    parser.add_argument("--port", type=int, default=8876)
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    parser.add_argument("--data-dir", type=Path, help="指定本地分析记录目录")
    parser.add_argument("--original-dir", type=Path, help="指定原始资料目录")
    parser.add_argument("--desktop", action="store_true", help="使用独立于应用包的桌面用户数据目录")
    parser.add_argument("--export-dir", type=Path, help="指定默认生成文件目录")
    parser.add_argument("--ready-file", type=Path, help="监听成功后原子写入本机服务就绪信息")
    parser.add_argument("--parent-stdin", action="store_true", help="桌面宿主的标准输入管道关闭后停止服务")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("端口必须为0—65535；0表示由系统分配空闲端口。")
    try:
        configure_runtime(args)
    except (OSError, ValueError) as exc:
        print(f"运行目录无法准备：{exc}", file=sys.stderr)
        return 1
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        print("缺少 openpyxl，请使用启动脚本或安装 requirements.txt。", file=sys.stderr)
        return 1
    server = None
    for port in ([0] if args.port == 0 else range(args.port, min(args.port + 10, 65536))):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if server is None:
        print("可用端口被占用，请通过 --port 指定其他端口。", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"\n零售通 v{VERSION}\n{url}\n关闭终端或按 Ctrl+C 停止服务。\n", flush=True)
    if args.open and not DESKTOP_MODE:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    ready_path = args.ready_file.expanduser().resolve() if args.ready_file else None
    ready_payload = None
    previous_signals = {}
    def stop_service(signum, frame):
        raise KeyboardInterrupt
    try:
        # SIGTERM is the normal native-app shutdown path. Raise out of the
        # serving loop; calling shutdown() on this same thread would deadlock.
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_signals[signum] = signal.signal(signum, stop_service)
        if ready_path is not None:
            ready_payload = {"url": url, "version": VERSION, "pid": os.getpid()}
            write_ready_file(ready_path, ready_payload)
        if DESKTOP_MODE and args.parent_stdin:
            watch_parent_stdin(server)
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
    except (OSError, ValueError) as exc:
        print(f"本地服务无法启动：{exc}", file=sys.stderr)
        return 1
    finally:
        server.server_close()
        remove_ready_file(ready_path, ready_payload)
        for signum, handler in previous_signals.items():
            signal.signal(signum, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
