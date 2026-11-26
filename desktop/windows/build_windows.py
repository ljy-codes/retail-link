"""Build the self-contained Windows app and a per-user installer."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import site
import struct
import subprocess
import sys
import time
import urllib.request
import zipfile
import zlib

WINDOWS = Path(__file__).resolve().parent
DESKTOP = WINDOWS.parent
DEV = DESKTOP.parent
ROOT = DEV
BUILD = DESKTOP / "build" / "windows"
sys.path.insert(0, str(DESKTOP))
import shared_payload as shared

RELEASE = json.loads(shared.read_bytes(DESKTOP / "release.json"))
PYTHON_URL = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip"
UTF8_BOM = b"\xef\xbb\xbf"


def validate_release(release):
    if not isinstance(release, dict):
        raise ValueError("发布配置必须是 JSON 对象。")
    if release.get("featureLevel") not in {"desktop-3", "desktop-4"}:
        raise ValueError("Windows 发版必须复用 desktop-3 或 desktop-4 功能基线。")
    mac = _version(release.get("macVersion"), "macVersion")
    windows = _version(release.get("windowsVersion"), "windowsVersion")
    if mac[0:2] != windows[0:2]:
        raise ValueError("Mac 与 Windows 的主次版本必须一致。")
    if mac[2] % 2 != 1:
        raise ValueError("Mac 补丁版本必须为奇数。")
    # The first Windows package for a Mac feature line is Mac.Z+1. Later
    # Windows-only maintenance deliveries advance by two while retaining the
    # latest verified Mac pair, without inventing an unbuilt Mac release.
    if windows[2] <= mac[2] or windows[2] % 2 != 0:
        raise ValueError("Windows 补丁版本必须是大于 Mac 补丁版本的偶数。")
    return release


def windows_metadata(release):
    checked = validate_release(release)
    return {
        "name": "零售通",
        "version": checked["windowsVersion"],
        "platform": "windows",
        "featureLevel": checked["featureLevel"],
        "pairedMacVersion": checked["macVersion"],
    }


def forbidden_payload_path(relative):
    path = Path(relative)
    parts = [part.lower() for part in path.parts]
    forbidden_names = {
        "__pycache__", "本地数据", "原始资料", "生成文件", "测试空间",
        ".git", ".pytest_cache", ".mypy_cache", "node_modules", "desktop",
    }
    if any(part in forbidden_names for part in parts):
        return True
    if "windows" in parts and "desktop" in parts:
        return True
    if path.suffix.lower() in {
        ".pyc", ".pyo", ".xlsx", ".xlsm", ".xls", ".sqlite3", ".sqlite",
        ".db", ".jsonl", ".csv", ".log", ".part", ".tmp",
    }:
        return True
    if any(part.startswith(("每日记录自测-", "价量适配自测-", "dataset-", "daily-export-"))
           for part in path.parts):
        return True
    return False


def ensure_powershell_utf8_bom(data):
    return data if data.startswith(UTF8_BOM) else UTF8_BOM + data


def powershell_script_bytes(source):
    return ensure_powershell_utf8_bom(shared.read_bytes(source))


def _version(value, field):
    if not isinstance(value, str):
        raise ValueError(f"{field} 缺失。")
    pieces = value.split(".")
    if len(pieces) != 3 or not all(piece.isdigit() for piece in pieces):
        raise ValueError(f"{field} 必须是三段数字版本。")
    return tuple(int(piece) for piece in pieces)


def run(*args, cwd=ROOT, env=None):
    command = [str(arg) for arg in args]
    print(">", subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, cwd=cwd, check=True, env=env)


def copy_tree(source, destination):
    shared.copy_tree(source, destination)


def find_dotnet():
    dotnet = shutil.which("dotnet")
    if dotnet:
        return dotnet
    candidate = Path(r"C:\Program Files\dotnet\dotnet.exe")
    if candidate.is_file():
        return str(candidate)
    raise RuntimeError("未找到 .NET SDK；请安装 .NET 10 SDK。")


def find_site_packages(explicit=None):
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    import sysconfig
    candidates.append(Path(sysconfig.get_paths().get("purelib", "")))
    user_site = site.getusersitepackages()
    if user_site:
        candidates.append(Path(user_site))
    candidates.extend(Path(entry) for entry in sys.path if entry)
    for candidate in candidates:
        if (candidate / "openpyxl").is_dir() and (candidate / "et_xmlfile").is_dir():
            return candidate
    raise RuntimeError("未找到 openpyxl 与 et_xmlfile；请通过 --python-packages 指定目录。")


def download_runtime(destination, source=None):
    shared.check_destination(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source:
        shared.copy_file(source, destination)
        return destination
    if destination.is_file():
        shared.checked_file(destination)
        if destination.stat().st_size > 1_000_000:
            return destination
    request = urllib.request.Request(PYTHON_URL, headers={"User-Agent": "RetailLink-Windows-Build/1.0"})
    print(f"下载 {PYTHON_URL}", flush=True)
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as stream:
        if response.headers.get("Content-Length"):
            shared.check_size(int(response.headers["Content-Length"]), PYTHON_URL)
        count = 0
        while chunk := response.read(1024 * 1024):
            count += len(chunk)
            shared.check_size(count, PYTHON_URL)
            stream.write(chunk)
    if destination.stat().st_size < 1_000_000:
        destination.unlink(missing_ok=True)
        raise RuntimeError("Python embeddable 下载不完整。")
    return destination


def prepare_runtime(runtime_zip, site_packages, destination):
    shared.extract_runtime_zip(runtime_zip, destination)
    pth = destination / "python312._pth"
    if not pth.is_file():
        raise RuntimeError("Python embeddable 缺少 python312._pth。")
    pth.write_text(
        "python312.zip\r\n.\r\nLib\\site-packages\r\nimport site\r\n",
        encoding="utf-8",
    )
    packages = destination / "Lib" / "site-packages"
    packages.mkdir(parents=True, exist_ok=True)
    for name in ("openpyxl", "et_xmlfile"):
        copy_tree(site_packages / name, packages / name)
    for pattern in ("openpyxl-*.dist-info", "et_xmlfile-*.dist-info"):
        for metadata in site_packages.glob(pattern):
            copy_tree(metadata, packages / metadata.name)
    licenses = DESKTOP / "licenses"
    if licenses.is_dir():
        copy_tree(licenses, destination / "licenses")
    python = destination / "python.exe"
    if not python.is_file():
        raise RuntimeError("内置 Python 解压失败。")
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"
    try:
        run(python, "-I", "-B", "-c",
            "import openpyxl,et_xmlfile,sqlite3,ssl,hashlib,zlib;"
            "print(openpyxl.__version__)", env=env)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("内置 Python/openpyxl 自检失败。") from exc


def build_app(dotnet, version, destination):
    shared.check_destination(destination)
    shared.tree_files(WINDOWS / "app")
    if destination.exists():
        shared.remove_build_tree(destination, BUILD)
    destination.mkdir(parents=True)
    run(
        dotnet, "publish", WINDOWS / "app" / "RetailLink.Windows.csproj",
        "-c", "Release", "-r", "win-x64", "--self-contained", "true",
        "-p:PublishSingleFile=true",
        "-p:IncludeNativeLibrariesForSelfExtract=true",
        "-p:EnableCompressionInSingleFile=true",
        "-p:DebugType=None", "-p:DebugSymbols=false",
        f"-p:Version={version}", f"-p:FileVersion={version}.0",
        f"-p:AssemblyVersion={version}.0", "-o", destination,
    )
    executable = destination / "零售通.exe"
    if not executable.is_file():
        raise RuntimeError("应用发布未生成 零售通.exe。")
    shared.tree_files(destination)
    for path in destination.iterdir():
        if path.name not in {"零售通.exe"}:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()


def build_payload(version, app_publish, runtime_zip, site_packages):
    payload = BUILD / "payload"
    shared.check_destination(payload)
    if payload.exists():
        shared.remove_build_tree(payload, BUILD)
    payload.mkdir(parents=True)
    shared.copy_file(app_publish / "零售通.exe", payload / "零售通.exe", root=app_publish)
    app = payload / "app"
    shared.copy_shared_payload(DEV, app, RELEASE["features"])
    shared.copy_file(WINDOWS / "使用说明.html", app / "使用说明.html", root=WINDOWS)
    metadata = windows_metadata(RELEASE)
    metadata["architecture"] = "x64"
    (app / "版本.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    prepare_runtime(runtime_zip, site_packages, payload / "runtime")
    uninstaller_source = WINDOWS / "uninstall" / "uninstall.ps1"
    uninstaller_target = payload / "uninstall.ps1"
    uninstaller_target.write_bytes(powershell_script_bytes(uninstaller_source))
    shutil.copystat(uninstaller_source, uninstaller_target)
    shared.validate_payload(payload)
    return payload


def smoke_backend(payload):
    smoke = BUILD / "smoke"
    shared.check_destination(smoke)
    if smoke.exists():
        shared.remove_build_tree(smoke, BUILD)
    data = smoke / "data"
    original = smoke / "original"
    export = smoke / "export"
    for path in (data, original, export):
        path.mkdir(parents=True)
    ready = smoke / "ready.json"
    python = payload / "runtime" / "python.exe"
    app = payload / "app"
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUTF8"] = "1"
    process = subprocess.Popen(
        [str(python), "-B", str(app / "server.py"), "--desktop", "--parent-stdin", "--port", "0",
         "--data-dir", str(data), "--original-dir", str(original), "--export-dir", str(export),
         "--ready-file", str(ready)],
        cwd=app, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=env,
    )
    try:
        deadline = time.time() + 40
        while time.time() < deadline:
            if process.poll() is not None:
                output = process.stdout.read().decode("utf-8", "replace") if process.stdout else ""
                raise RuntimeError(f"打包后端提前退出：\n{output}")
            if ready.is_file():
                payload_data = json.loads(shared.read_bytes(ready, smoke))
                if payload_data.get("pid") != process.pid:
                    raise RuntimeError("打包后端 PID 校验失败。")
                if payload_data.get("version") != windows_metadata(RELEASE)["version"]:
                    raise RuntimeError("打包后端版本校验失败。")
                from urllib.error import HTTPError
                from urllib.request import urlopen
                with urlopen(payload_data["url"] + "/api/health", timeout=10) as response:
                    health = json.loads(response.read().decode("utf-8"))
                if health.get("desktop") is not True or health.get("version") != payload_data["version"]:
                    raise RuntimeError("打包后端健康检查失败。")
                if "data_health" not in (health.get("capabilities") or []):
                    raise RuntimeError("打包后端缺少数据健康能力。")
                # Exercise a real backend import, not only the dependency-free health route.
                with urlopen(payload_data["url"] + "/api/import/templates", timeout=10) as response:
                    templates = json.loads(response.read().decode("utf-8"))
                if not isinstance(templates.get("templates"), list):
                    raise RuntimeError("打包后端的业务模块检查失败。")
                try:
                    urlopen(payload_data["url"] + "/api/health/report", timeout=10)
                except HTTPError as error:
                    if error.code != 400:
                        raise RuntimeError(f"打包数据健康接口返回了异常状态：{error.code}") from error
                    message = json.loads(error.read().decode("utf-8")).get("error", "")
                    if "分析快照" not in message:
                        raise RuntimeError("打包数据健康接口缺少快照校验。") from error
                else:
                    raise RuntimeError("打包数据健康接口未校验快照参数。")
                try:
                    urlopen(payload_data["url"] + "/api/sample", timeout=10)
                except HTTPError as error:
                    if error.code != 400:
                        raise RuntimeError(f"打包后端缺少样例时返回了异常状态：{error.code}") from error
                    message = json.loads(error.read().decode("utf-8")).get("error", "")
                    if "未附带样例" not in message:
                        raise RuntimeError("打包后端缺少样例时未返回可识别提示。") from error
                else:
                    raise RuntimeError("打包后端缺少样例时未返回 400。")
                return
            time.sleep(0.1)
        raise RuntimeError("打包后端启动超时。")
    finally:
        if process.stdin:
            process.stdin.close()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def zip_payload(payload, destination):
    files = shared.validate_payload(payload)
    # ZIP_STORED upper bound plus headers: refuse before touching an old archive.
    shared.check_size(sum(p.stat().st_size + 1024 for p in files) + 1024, destination)
    shared.check_destination(destination)
    if destination.exists():
        shared.checked_file(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            relative = path.relative_to(payload).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, shared.read_bytes(path, payload), compresslevel=9)
    shared.checked_file(destination)
    return destination


def build_installer(dotnet, version, payload_zip, destination):
    shared.check_destination(destination)
    shared.checked_file(payload_zip)
    with zipfile.ZipFile(payload_zip) as archive:
        shared.archive_members(archive)
    shared.tree_files(WINDOWS / "installer")
    if destination.exists():
        shared.remove_build_tree(destination, BUILD)
    destination.mkdir(parents=True)
    run(
        dotnet, "publish", WINDOWS / "installer" / "RetailLink.Setup.csproj",
        "-c", "Release", "-r", "win-x64", "--self-contained", "true",
        "-p:PublishSingleFile=true",
        "-p:IncludeNativeLibrariesForSelfExtract=true",
        "-p:EnableCompressionInSingleFile=true",
        "-p:DebugType=None", "-p:DebugSymbols=false",
        f"-p:Version={version}", f"-p:FileVersion={version}.0",
        f"-p:AssemblyVersion={version}.0",
        f"-p:PayloadPath={payload_zip.resolve()}",
        "-o", destination,
    )
    source = destination / "RetailLink.Setup.exe"
    if not source.is_file():
        raise RuntimeError("安装程序发布失败。")
    shared.tree_files(destination)
    final = destination / f"零售通-v{version}-windows-x64-setup.exe"
    source.replace(final)
    for path in destination.iterdir():
        if path != final:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    return final


def publish(delivery, installer):
    staging = BUILD / "delivery"
    shared.check_destination(staging)
    if staging.exists():
        shared.remove_build_tree(staging, BUILD)
    staging.mkdir(parents=True)
    version = windows_metadata(RELEASE)["version"]
    shared.copy_file(installer, staging / installer.name)
    shared.copy_file(WINDOWS / "使用说明.html", staging / f"零售通-v{version}-使用说明.html")
    if len(list(staging.iterdir())) != 2:
        raise RuntimeError("Windows 交付目录必须仅有安装包和使用说明。")
    destination = shared.publish_delivery(staging, delivery, "Windows")
    for path in sorted(destination.iterdir()):
        digest = shared.file_digest(path)
        print(f"{path.name}\t{path.stat().st_size}\t{digest}")
    return destination


def write_icon(path):
    shared.check_destination(path)
    size = 256
    pixels = bytearray([0, 0, 0, 0] * size * size)

    def set_pixel(x, y, color):
        if 0 <= x < size and 0 <= y < size:
            index = (y * size + x) * 4
            pixels[index:index + 4] = bytes(color)

    def rectangle(x0, y0, x1, y1, radius, color):
        for y in range(max(0, y0), min(size, y1 + 1)):
            for x in range(max(0, x0), min(size, x1 + 1)):
                cx = min(max(x, x0 + radius), x1 - radius)
                cy = min(max(y, y0 + radius), y1 - radius)
                if (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2:
                    set_pixel(x, y, color)

    # A light rounded tile with a strong red rising line remains legible at the
    # 16 px sizes used by Explorer, the taskbar and the Start menu.
    rectangle(13, 18, 243, 247, 50, (45, 12, 18, 48))
    rectangle(12, 12, 243, 241, 50, (218, 38, 57, 255))
    rectangle(23, 23, 232, 230, 40, (255, 248, 245, 255))

    for x0, x1, top, color in (
        (48, 69, 158, (248, 205, 211, 255)),
        (84, 105, 133, (244, 179, 188, 255)),
        (120, 141, 146, (239, 151, 164, 255)),
        (156, 177, 104, (230, 112, 129, 255)),
        (192, 213, 78, (218, 38, 57, 255)),
    ):
        rectangle(x0, top, x1, 203, 8, color)

    points = ((48, 184), (82, 151), (128, 128), (170, 84), (210, 58))
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        steps = max(abs(x1 - x0), abs(y1 - y0))
        for step in range(steps + 1):
            x = round(x0 + (x1 - x0) * step / steps)
            y = round(y0 + (y1 - y0) * step / steps)
            rectangle(x - 7, y - 7, x + 7, y + 7, 7, (210, 35, 54, 255))
    for x, y in points:
        rectangle(x - 10, y - 10, x + 10, y + 10, 10, (255, 248, 245, 255))
        rectangle(x - 6, y - 6, x + 6, y + 6, 6, (210, 35, 54, 255))

    raw = bytearray()
    for row in range(size):
        raw.append(0)
        raw.extend(pixels[row * size * 4:(row + 1) * size * 4])

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
           + chunk(b"IEND", b""))
    ico = (struct.pack("<HHH", 0, 1, 1)
           + struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png), 22)
           + png)
    path.write_bytes(ico)


def clean_build():
    shared.remove_build_tree(BUILD, DESKTOP / "build" / "windows")


def main():
    validate_release(RELEASE)
    parser = argparse.ArgumentParser(description="构建零售通 Windows 安装包")
    parser.add_argument("--python-runtime-zip", type=Path)
    parser.add_argument("--python-packages", type=Path)
    parser.add_argument("--dotnet", default=None)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--delivery-dir", type=Path, default=shared.default_delivery_dir(ROOT, "Windows"))
    parser.add_argument("--keep-build", action="store_true")
    parser.add_argument("--write-icon-only", action="store_true")
    args = parser.parse_args()
    write_icon(WINDOWS / "RetailLink.ico")
    if args.write_icon_only:
        return

    shared.check_destination(BUILD)
    BUILD.mkdir(parents=True, exist_ok=True)
    dotnet = find_dotnet() if not args.dotnet else args.dotnet
    site_packages = find_site_packages(args.python_packages)
    runtime_zip = download_runtime(BUILD / "cache" / "python-3.12.10-embed-amd64.zip",
                                   args.python_runtime_zip)
    version = windows_metadata(RELEASE)["version"]
    app_publish = BUILD / "app-publish"
    build_app(dotnet, version, app_publish)
    payload = build_payload(version, app_publish, runtime_zip, site_packages)
    smoke_backend(payload)
    payload_zip = zip_payload(payload, BUILD / "payload.zip")
    install_destination = BUILD / "installer-publish"
    installer = build_installer(dotnet, version, payload_zip, install_destination)
    if args.publish:
        delivery = publish(args.delivery_dir, installer)
        print(f"交付完成：{delivery}")
        if not args.keep_build:
            clean_build()
    else:
        print(f"构建完成：{installer}")


if __name__ == "__main__":
    main()
