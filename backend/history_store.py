"""Persistent, immutable analysis snapshots with a recoverable recycle bin.

This module owns JSON snapshots only. Workbook registration and locating source
files belong to the HTTP service. It never modifies or deletes an Excel source.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


class HistoryStore:
    """Thread-safe file storage for complete analysis results.

    Successful saves create a new snapshot and never replace an existing ID in
    either the active directory or recycle bin. Returned dictionaries are fresh
    JSON values, so later mutations cannot modify the stored snapshot.
    """

    SUMMARY_FIELDS = ("id", "filename", "asOf", "period", "generatedAt", "summary", "appVersion")
    _ID_PATTERN = re.compile(r"[0-9]{1,64}\Z", flags=re.ASCII)

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.history_dir = self.data_dir / "history"
        self.deleted_dir = self.data_dir / "history_deleted"
        self._lock = threading.RLock()
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self.deleted_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def _identifier(cls, value) -> str:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError("历史记录 ID 必须为纯数字。")
        identifier = str(value)
        if not cls._ID_PATTERN.fullmatch(identifier):
            raise ValueError("历史记录 ID 必须为 1—64 位 ASCII 数字。")
        return identifier

    @staticmethod
    def _exists(path: Path) -> bool:
        # Dangling symlinks must also count as occupied; no existing path should
        # be replaced, even if it is not a valid snapshot.
        return path.exists() or path.is_symlink()

    def _path(self, identifier, deleted=False) -> Path:
        value = self._identifier(identifier)
        return (self.deleted_dir if deleted else self.history_dir) / (value + ".json")

    @staticmethod
    def _sync_directory(directory: Path):
        """Best-effort directory fsync; not every OS/filesystem supports it."""
        descriptor = None
        try:
            descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _require_file(path: Path):
        if path.is_symlink():
            raise ValueError("历史记录不能是符号链接。")
        if not path.is_file():
            raise ValueError("历史记录不存在，或已经移至其他列表。")

    def _read(self, path: Path) -> dict:
        self._require_file(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("历史记录无法读取，文件可能不完整；原文件已保留。") from exc
        if not isinstance(data, dict):
            raise ValueError("历史记录内容应为完整的分析对象。")
        if str(data.get("id")) != path.stem:
            raise ValueError("历史记录的 ID 与文件名不一致。")
        return data

    def _create(self, path: Path, payload: bytes):
        """Publish a fully flushed file atomically without replacing a target.

        The temporary file and target are on the same filesystem. Linking the
        completed file publishes it in one operation and fails if the target is
        already present, unlike replace()/rename() which may overwrite data.
        """
        temporary = None
        try:
            descriptor, name = tempfile.mkstemp(prefix=".pending-", suffix=".tmp", dir=path.parent)
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise ValueError("相同 ID 的历史记录已存在，不能覆盖。") from exc
            self._sync_directory(path.parent)
        except ValueError:
            raise
        except OSError as exc:
            raise ValueError("无法保存历史记录，请检查本地数据目录。") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def save(self, result: dict) -> dict:
        if not isinstance(result, dict):
            raise ValueError("分析快照必须是对象。")
        with self._lock:
            # Round-trip a complete JSON snapshot: no field whitelist can drop
            # sources, sourceInfo, issues, configuration, or manual overrides.
            try:
                snapshot = json.loads(json.dumps(result, ensure_ascii=False, allow_nan=False))
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("分析快照包含不能保存的值；数值必须有限且可写入 JSON。") from exc
            if "id" in snapshot and snapshot["id"] is not None:
                identifier = self._identifier(snapshot["id"])
                if self._exists(self._path(identifier)) or self._exists(self._path(identifier, deleted=True)):
                    raise ValueError("相同 ID 的历史记录已存在，不能覆盖。")
            else:
                next_id = time.time_ns()
                while self._exists(self._path(str(next_id))) or self._exists(self._path(str(next_id), deleted=True)):
                    next_id += 1
                identifier = str(next_id)
            snapshot["id"] = identifier
            snapshot.setdefault("generatedAt", datetime.now(timezone.utc).isoformat())
            snapshot.setdefault("appVersion", None)
            try:
                payload = json.dumps(snapshot, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("分析快照无法序列化。") from exc
            self._create(self._path(identifier), payload)
            return snapshot

    def list(self, offset=0, limit=50, deleted=False) -> dict:
        try:
            offset = max(0, int(offset))
            limit = max(1, min(100, int(limit)))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("历史分页参数必须为整数。") from exc
        with self._lock:
            directory = self.deleted_dir if deleted else self.history_dir
            paths = [path for path in directory.glob("*.json")
                     if self._ID_PATTERN.fullmatch(path.stem) and path.is_file() and not path.is_symlink()]
            paths.sort(key=lambda path: (int(path.stem), path.stem), reverse=True)
            items = []
            for path in paths[offset:offset + limit]:
                try:
                    data = self._read(path)
                    item = {key: data.get(key) for key in self.SUMMARY_FIELDS}
                except ValueError as exc:
                    # A damaged file remains visible and recoverable; it is not
                    # silently omitted from counts or automatically discarded.
                    item = {key: None for key in self.SUMMARY_FIELDS}
                    item.update({"id": path.stem, "filename": "无法读取的历史记录", "error": str(exc)})
                items.append(item)
            return {"items": items, "total": len(paths), "hasMore": offset + len(items) < len(paths)}

    def get(self, id, deleted=False) -> dict:
        with self._lock:
            return self._read(self._path(id, deleted=deleted))

    def _move(self, identifier, deleting: bool) -> dict:
        value = self._identifier(identifier)
        source = self._path(value, deleted=not deleting)
        destination = self._path(value, deleted=deleting)
        self._require_file(source)
        if self._exists(destination):
            raise ValueError("目标列表中已存在相同 ID，不能覆盖历史记录。")
        try:
            # link + unlink never replaces a pre-existing snapshot. Both
            # directories are local siblings. A crash between the two can leave
            # two recoverable copies, but can never lose the only saved copy.
            os.link(source, destination)
            try:
                source.unlink()
            except OSError:
                # The move did not complete: retain the original and remove our
                # added link, so a retry has a clear source of truth.
                destination.unlink(missing_ok=True)
                raise
            self._sync_directory(source.parent)
            self._sync_directory(destination.parent)
        except FileExistsError as exc:
            raise ValueError("目标列表中已存在相同 ID，不能覆盖历史记录。") from exc
        except FileNotFoundError as exc:
            raise ValueError("历史记录不存在，或已被移动。") from exc
        except OSError as exc:
            raise ValueError("无法移动历史记录，请检查本地数据目录。") from exc
        return {"id": value, "status": "deleted" if deleting else "restored"}

    def delete(self, id) -> dict:
        with self._lock:
            return self._move(id, deleting=True)

    def restore(self, id) -> dict:
        with self._lock:
            return self._move(id, deleting=False)
