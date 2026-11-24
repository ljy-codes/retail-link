"""Atomic publication without replacement, including filesystems without links.

The source is always retained. On a filesystem without hard links we finish and
flush a private copy in the destination directory, then publish it with the OS's
exclusive rename primitive. There is no check-then-replace or visible partial file.
"""
from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile


_LINK_UNAVAILABLE = {errno.EXDEV, errno.EPERM, errno.ENOSYS, errno.EMLINK,
                     errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}


def _rename_exclusive(source, destination):
    """Rename an already complete stage; refuse any existing destination."""
    if os.name == "nt":
        # Windows rename does not replace an existing file or directory.
        os.rename(source, destination)
        return
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        function = getattr(library, "renamex_np", None)
        if function is None:
            raise OSError(errno.ENOTSUP, "Atomic exclusive rename is unavailable", os.fspath(destination))
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        result = function(os.fsencode(source), os.fsencode(destination), 4)  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        function = getattr(library, "renameat2", None)
        if function is None:
            raise OSError(errno.ENOTSUP, "Atomic exclusive rename is unavailable", os.fspath(destination))
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        result = function(-100, os.fsencode(source), -100, os.fsencode(destination), 1)  # AT_FDCWD, RENAME_NOREPLACE
    else:
        raise OSError(errno.ENOTSUP, "Atomic exclusive rename is unavailable", os.fspath(destination))
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), os.fspath(destination))


def publish_no_clobber(source, destination):
    """Publish a complete regular file and return its known (device, inode).

    The returned identity belongs to the object being published, captured before
    publication. Reading destination.stat() after publication would instead risk
    accepting a file concurrently replaced by someone else.
    """
    source, destination = Path(source), Path(destination)
    original = source.lstat()
    if not stat.S_ISREG(original.st_mode):
        raise OSError(errno.EINVAL, "Publication source must be a regular file", os.fspath(source))
    try:
        os.link(source, destination)
        return original.st_dev, original.st_ino
    except OSError as error:
        if error.errno not in _LINK_UNAVAILABLE:
            raise
    descriptor, name = tempfile.mkstemp(prefix=".publish-", suffix=".tmp", dir=destination.parent)
    stage = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            # O_NOFOLLOW keeps the copy path consistent with the regular-file
            # contract even if the source path is changed during publication.
            source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(source_fd, "rb") as incoming:
                info = os.fstat(incoming.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise OSError(errno.EINVAL, "Publication source must be a regular file", os.fspath(source))
                shutil.copyfileobj(incoming, output, length=1024 * 1024)
                if os.name != "nt":
                    try:
                        os.fchmod(output.fileno(), stat.S_IMODE(info.st_mode))
                    except OSError as error:
                        if error.errno not in _LINK_UNAVAILABLE | {errno.EINVAL}:
                            raise
            output.flush()
            os.fsync(output.fileno())
            published = os.fstat(output.fileno())
            identity = published.st_dev, published.st_ino
        _rename_exclusive(stage, destination)
        return identity
    finally:
        stage.unlink(missing_ok=True)
