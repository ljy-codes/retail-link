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
sys.dont_write_bytecode = True
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse
import webbrowser
import zipfile

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
WEB_DIR = APP_DIR / "web"
DATA_DIR = PROJECT_DIR / "本地数据"
ORIGINAL_DIR = PROJECT_DIR / "原始资料"
SAMPLE = ORIGINAL_DIR / "样例.xlsx"
VERSION = "0.1.1"
MAX_UPLOAD = 30 * 1024 * 1024
MAX_BODY = (MAX_UPLOAD * 4 // 3) + 1024 * 1024
WORKBOOKS: dict[str, dict] = {}
WORKBOOK_LOCK = threading.RLock()
HISTORY_STORES: dict[Path, object] = {}
HISTORY_LOCK = threading.RLock()


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
        }
    inspected.update({"workbookId": workbook_id, "filename": display_name or path.name,
                      "sourcePath": str(path), "sha256": digest})
    return inspected


def get_workbook(workbook_id):
    with WORKBOOK_LOCK:
        entry = WORKBOOKS.get(str(workbook_id))
    if not entry:
        raise ValueError("文件会话已失效，请重新选择 Excel。")
    if not entry["path"].is_file():
        raise ValueError("原文件已移动或删除，请重新选择。")
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

    def dispatch_daily(self, method, parsed, body=None):
        if parsed.path != "/api/daily" and not parsed.path.startswith("/api/daily/"):
            return False
        try:
            from backend.daily_records import handle_daily_request
        except ImportError:
            self.send_json({"error": "每日售价与销量模块尚未就绪。"}, 503)
            return True
        return handle_daily_request(handler=self, method=method, path=parsed.path,
                                    query=parse_qs(parsed.query), body=body,
                                    data_dir=DATA_DIR, lookup=get_workbook)

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/health":
                return self.send_json({"app": "零售通", "version": VERSION,
                                       "sampleAvailable": SAMPLE.is_file(),
                                       "defaultDirectory": str(ORIGINAL_DIR),
                                       "capabilities": ["excel", "directory", "manual", "mapping", "history"]})
            if self.dispatch_daily("GET", parsed):
                return
            if parsed.path == "/api/sample":
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
            if self.dispatch_daily("POST", parsed, body):
                return
            restore_match = re.fullmatch(r"/api/history/([0-9]{1,64})/restore", route)
            if restore_match:
                return self.send_json(history_store().restore(restore_match.group(1)))
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
                result = engine().analyze_workbook(entry["path"], config)
                if not result.get("products"):
                    raise ValueError("没有识别到商品数据。请在页面配置所选文件的工作表、表头行和商品列，再重新分析。")
                result["filename"] = entry["filename"]
                result["workbookId"] = str(body.get("workbookId"))
                result["generatedAt"] = datetime.now(timezone.utc).isoformat()
                result["id"] = str(time.time_ns())
                result["appVersion"] = VERSION
                result["sourceInfo"] = {"path": str(entry["path"]), "sha256": entry["sha256"],
                                        "filename": entry["filename"]}
                return self.send_json(history_store().save(result))
            self.send_json({"error": "接口不存在。"}, 404)
        except (ValueError, FileNotFoundError, KeyError, PermissionError) as exc:
            self.send_json({"error": str(exc)}, 400)
        except BrokenPipeError:
            pass
        except Exception as exc:
            traceback.print_exc()
            self.send_json({"error": f"分析失败：{exc}"}, 500)

    def do_DELETE(self):
        try:
            if not self.origin_allowed():
                return
            parsed = urlparse(self.path)
            if self.dispatch_daily("DELETE", parsed):
                return
            delete_match = re.fullmatch(r"/api/history/([0-9]{1,64})", parsed.path)
            if delete_match:
                return self.send_json(history_store().delete(delete_match.group(1)))
            self.send_json({"error": "接口不存在。"}, 404)
        except (ValueError, FileNotFoundError, KeyError, PermissionError) as exc:
            self.send_json({"error": str(exc)}, 400)
        except BrokenPipeError:
            pass
        except Exception as exc:
            traceback.print_exc()
            self.send_json({"error": f"删除失败：{exc}"}, 500)


def main():
    global DATA_DIR, ORIGINAL_DIR, SAMPLE
    parser = argparse.ArgumentParser(description="零售通 · 电商看盘")
    parser.add_argument("--port", type=int, default=8876)
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    parser.add_argument("--data-dir", type=Path, help="指定本地分析记录目录")
    parser.add_argument("--original-dir", type=Path, help="指定原始资料目录")
    args = parser.parse_args()
    if args.data_dir:
        DATA_DIR = args.data_dir.expanduser().resolve()
    if args.original_dir:
        ORIGINAL_DIR = args.original_dir.expanduser().resolve()
        SAMPLE = ORIGINAL_DIR / "样例.xlsx"
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        print("缺少 openpyxl，请使用启动脚本或安装 requirements.txt。", file=sys.stderr)
        return 1
    server = None
    for port in range(args.port, args.port + 10):
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
    if args.open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
