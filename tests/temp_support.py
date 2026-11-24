"""Portable temporary directories for isolated Windows and POSIX tests."""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import shutil
import stat


class TemporaryDirectory:
    """Create a writable temporary directory on restricted Windows hosts.

    ``tempfile.mkdtemp`` requests mode 0o700. Some Windows sandboxes translate
    that mode into an ACL that the same process cannot traverse. Test fixtures
    need the normal user-writable directory ACL instead.
    """

    def __init__(self, prefix="tmp-", directory=None):
        parent = Path(directory or os.environ.get("TEMP") or ".")
        parent.mkdir(parents=True, exist_ok=True)
        for _ in range(100):
            path = parent / f"{prefix}{secrets.token_hex(4)}"
            try:
                path.mkdir(mode=0o777)
                break
            except FileExistsError:
                continue
        else:
            raise FileExistsError(f"无法创建唯一临时目录：{parent}")
        self.name = str(path)
        self.path = path

    def cleanup(self):
        def reset(function, target, _error):
            os.chmod(target, stat.S_IWRITE)
            function(target)

        if self.path.exists():
            shutil.rmtree(self.path, onexc=reset)

    def __enter__(self):
        return self.name

    def __exit__(self, *_):
        self.cleanup()

    def __del__(self):
        try:
            self.cleanup()
        except Exception:
            pass


def temporary_directory(prefix="tmp-", directory=None):
    return TemporaryDirectory(prefix=prefix, directory=directory)
