"""Shared desktop build I/O, source inventory and conservative delivery publication."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import stat
import tempfile
import zipfile

MAX_FILE_SIZE = 314572800
MANIFEST_NAME = "shared-source-manifest.json"
_CACHE_NAMES = {"__pycache__", ".DS_Store", "Thumbs.db"}
_PRIVATE_NAMES = {
    "本地数据", "原始资料", "生成文件", "测试空间", "测试数据", "导入记录",
    "history", "history_deleted", "import_sessions", "node_modules", ".git",
    ".pytest_cache", ".mypy_cache", "operations", "writeback",
}
_PRIVATE_SUFFIXES = {
    ".xlsx", ".xlsm", ".xls", ".sqlite3", ".sqlite", ".db", ".jsonl",
    ".csv", ".log", ".part", ".tmp",
}
_SYSTEM_ALIASES = {
    "/var": "/private/var",
    "/tmp": "/private/tmp",
}


def check_size(size, label):
    if size > MAX_FILE_SIZE:
        raise ValueError(f"文件超过 300 MiB，禁止读取、复制、哈希或打包：{label}")


def _linked(path):
    path = Path(path)
    if path.is_symlink():
        # macOS exposes temporary directories through root-owned aliases such as
        # /var -> /private/var. These are trusted system paths, not user-selected
        # links that could redirect a build destination outside its workspace.
        alias = _SYSTEM_ALIASES.get(str(path))
        if alias and path.lstat().st_uid == 0 and path.resolve() == Path(alias):
            return False
        return True
    return getattr(path, "is_junction", lambda: False)()


def check_destination(path):
    """Never write through an existing symlink/junction in a destination path."""
    path = Path(path).absolute()
    for parent in (path, *path.parents):
        if _linked(parent):
            raise ValueError(f"目标路径包含链接：{parent}")
    return path


def checked_file(path, root=None):
    path = Path(path)
    root = Path(root) if root is not None else path.parent
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"文件链接越界：{path}")
    metadata = path.stat()
    check_size(metadata.st_size, path)
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"不是普通文件：{path}")
    return path


def read_bytes(path, root=None):
    path = checked_file(path, root)
    preflight_archive(path)
    with path.open("rb") as stream:
        check_size(os.fstat(stream.fileno()).st_size, path)
        data = stream.read(MAX_FILE_SIZE + 1)
    check_size(len(data), path)
    return data


def file_digest(path):
    return hashlib.sha256(read_bytes(path)).hexdigest()


def _private(relative):
    path = PurePosixPath(str(relative).replace("\\", "/"))
    return (any(part.lower() in _PRIVATE_NAMES for part in path.parts)
            or path.suffix.lower() in _PRIVATE_SUFFIXES)


def tree_files(root, *, payload=False):
    """Preflight the entire tree, including ignored caches, before content I/O."""
    root = Path(root)
    check_destination(root)
    if not root.is_dir():
        raise ValueError(f"目录不存在：{root}")
    files = []
    seen = set()
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in [*dirs, *names]:
            path = Path(directory) / name
            key = path.relative_to(root).as_posix().casefold()
            if key in seen:
                raise ValueError(f"目录含大小写冲突路径：{path}")
            seen.add(key)
            if _linked(path):
                raise ValueError(f"载荷目录不允许链接：{path}")
            if path.is_file():
                checked_file(path, root)
                files.append(path)
            elif not path.is_dir():
                raise ValueError(f"载荷含特殊文件：{path}")
            if payload and _private(path.relative_to(root).as_posix()):
                raise ValueError(f"载荷含业务数据或私有目录：{path}")
    for path in files:
        preflight_archive(path)
    return sorted(files)


def copy_file(source, destination, *, root=None):
    source = checked_file(source, root)
    destination = check_destination(destination)
    # Read only after both source size and destination traversal have been checked.
    data = read_bytes(source, root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as stream:
        stream.write(data)
    shutil.copystat(source, destination)
    return destination


def copy_tree(source, destination, *, ignore=None):
    source, destination = Path(source), check_destination(destination)
    files = tree_files(source)
    selected = []
    for path in files:
        relative = path.relative_to(source)
        if ignore and ignore(relative):
            continue
        if any(p in _CACHE_NAMES for p in relative.parts) or path.suffix in {".pyc", ".pyo"}:
            continue
        selected.append((path, destination / relative))
    for path, target in selected:
        copy_file(path, target, root=source)


def _shared_sources(repo):
    repo = Path(repo)
    files = [checked_file(repo / "server.py", repo)]
    for name, suffixes in (
        ("backend", {".py"}),
        ("web", {".js", ".css", ".html", ".svg", ".png", ".ico", ".woff", ".woff2", ".ttf"}),
    ):
        directory = repo / name
        if not directory.is_dir():
            raise ValueError(f"缺少共享源码目录：{directory}")
        for path in tree_files(directory):
            relative = path.relative_to(repo)
            if any(p in _CACHE_NAMES for p in relative.parts) or path.suffix in {".pyc", ".pyo"}:
                continue
            if _private(relative.as_posix()) or path.suffix.lower() not in suffixes:
                raise ValueError(f"共享源码含非源码或业务文件：{relative}")
            files.append(path)
    return sorted(files)


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def copy_shared_payload(repo, destination, features):
    repo, destination = Path(repo), check_destination(destination)
    sources = _shared_sources(repo)
    if destination.exists() and list(destination.iterdir()):
        raise ValueError(f"共享载荷目标必须为空：{destination}")
    feature_bytes = _json_bytes(features)
    check_size(len(feature_bytes), "功能清单.json")
    destination.mkdir(parents=True, exist_ok=True)
    for source in sources:
        copy_file(source, destination / source.relative_to(repo), root=repo)
    (destination / "功能清单.json").write_bytes(feature_bytes)
    files = {}
    for relative in [p.relative_to(repo).as_posix() for p in sources] + ["功能清单.json"]:
        data = read_bytes(destination / relative, destination)
        files[relative] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    (destination / MANIFEST_NAME).write_bytes(_json_bytes({"schemaVersion": 1, "files": files}))
    verify_shared_payload(destination)
    return files


def verify_shared_payload(destination):
    destination = Path(destination)
    manifest = json.loads(read_bytes(destination / MANIFEST_NAME, destination))
    if manifest.get("schemaVersion") != 1:
        raise ValueError("共享源码 manifest 版本不支持。")
    actual = {}
    sources = _shared_sources(destination) + [checked_file(destination / "功能清单.json", destination)]
    for path in sources:
        data = read_bytes(path, destination)
        actual[path.relative_to(destination).as_posix()] = {
            "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        }
    if actual != manifest.get("files"):
        raise ValueError("共享源码与 manifest 不一致，请重新构建。")
    return manifest


def archive_members(archive, *, depth=0):
    if depth > 4:
        raise ValueError("归档嵌套过深，拒绝读取或打包。")
    infos = archive.infolist()
    seen = set()
    for info in infos:
        check_size(info.file_size, info.filename)
        check_size(info.compress_size, info.filename)
        name = info.filename.replace("\\", "/")
        path = PurePosixPath(name)
        if (not name or path.is_absolute() or PureWindowsPath(name).drive
                or ".." in path.parts or "\x00" in name or ":" in name
                or any(p.endswith((" ", ".")) for p in path.parts)):
            raise ValueError(f"归档成员路径不安全：{info.filename}")
        kind = stat.S_IFMT(info.external_attr >> 16)
        if kind not in {0, stat.S_IFREG, stat.S_IFDIR} or info.flag_bits & 1:
            raise ValueError(f"归档含链接、特殊文件或加密成员：{info.filename}")
        key = name.rstrip("/").casefold()
        if key in seen or _private(name):
            raise ValueError(f"归档含重复成员或业务数据：{info.filename}")
        seen.add(key)
    # Check every outer member's metadata before opening even one nested ZIP.
    for info in infos:
        if not info.is_dir() and PurePosixPath(info.filename).suffix.lower() == ".zip":
            with archive.open(info) as stream:
                with zipfile.ZipFile(stream) as nested:
                    archive_members(nested, depth=depth + 1)
    return infos


def preflight_archive(path):
    if Path(path).suffix.lower() == ".zip":
        with zipfile.ZipFile(checked_file(path)) as archive:
            archive_members(archive)


def extract_runtime_zip(source, destination):
    checked_file(source)
    destination = check_destination(destination)
    with zipfile.ZipFile(source) as archive:
        infos = archive_members(archive)
        for info in infos:
            target = destination / info.filename.replace("\\", "/")
            check_destination(target)
        destination.mkdir(parents=True, exist_ok=True)
        for info in infos:
            target = destination / info.filename.replace("\\", "/")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                with archive.open(info) as stream:
                    data = stream.read(MAX_FILE_SIZE + 1)
                check_size(len(data), info.filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)


def validate_payload(root):
    return tree_files(root, payload=True)


def remove_build_tree(path, build_root):
    path, build_root = check_destination(path), check_destination(build_root)
    if not path.resolve().is_relative_to(build_root.resolve()):
        raise ValueError(f"拒绝清理构建目录之外的路径：{path}")
    if path.exists():
        # rmtree unlinks symlinks/junctions without following them. In particular,
        # our DMG staging directory intentionally contains an /Applications link.
        shutil.rmtree(path)


def default_delivery_dir(repo, platform):
    repo = Path(repo)
    root = repo.parent if repo.name == "源码" else repo
    return root / "产品交付" / platform


def publish_delivery(staging, destination, platform):
    """Publish a verified pair; refuse unknown targets instead of deleting them."""
    staging, destination = Path(staging), check_destination(destination)
    files = tree_files(staging)
    pattern = (r"零售通-v\d+\.\d+\.\d+-windows-x64-setup\.exe" if platform == "Windows"
               else r"零售通-v\d+\.\d+\.\d+-mac-Intel\.dmg")
    manual = r"零售通-v\d+\.\d+\.\d+-使用说明\.html"

    def known(path):
        return path.is_file() and not _linked(path) and bool(
            re.fullmatch(pattern, path.name) or re.fullmatch(manual, path.name))

    def check_pair(paths):
        if (len(paths) != 2 or not all(known(p) for p in paths)
                or not any(re.fullmatch(pattern, p.name) for p in paths)
                or not any(re.fullmatch(manual, p.name) for p in paths)):
            raise ValueError("发布暂存必须恰好包含本平台安装包和使用说明。")
        versions = {re.match(r"零售通-v(\d+\.\d+\.\d+)-", p.name).group(1) for p in paths}
        if len(versions) != 1:
            raise ValueError("安装包与使用说明版本不一致。")

    check_pair(files)
    if (destination.resolve().is_relative_to(staging.resolve())
            or staging.resolve().is_relative_to(destination.resolve())):
        raise ValueError("发布目标和暂存目录不能互相包含。")
    if destination.exists():
        if not destination.is_dir() or any(not known(p) for p in destination.iterdir()):
            raise ValueError(f"发布目标含未知文件，已保留现场：{destination}")
        tree_files(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".retail-publish-", dir=destination.parent) as temporary:
        prepared = Path(temporary) / "new"
        prepared.mkdir()
        for path in files:
            copy_file(path, prepared / path.name, root=staging)
        # Keep the backup outside TemporaryDirectory: an interrupted rollback
        # must never cause its automatic recursive cleanup to erase old files.
        previous = destination.parent / (".retail-previous-" + Path(temporary).name)
        if destination.exists():
            # Recheck immediately before moving the existing delivery.
            if any(not known(p) for p in destination.iterdir()):
                raise ValueError(f"发布目标已变化：{destination}")
            destination.rename(previous)
        try:
            prepared.rename(destination)
        except Exception:
            if previous.exists() and not destination.exists():
                previous.rename(destination)
            raise
        if previous.exists():
            # Only unlink recognized artifacts. Never recursively erase unknown files.
            for path in previous.iterdir():
                if not known(path):
                    raise ValueError(f"旧交付出现未知文件，已保留：{previous}")
            for path in previous.iterdir():
                path.unlink()
            previous.rmdir()
    return destination
