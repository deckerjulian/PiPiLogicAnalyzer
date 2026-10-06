# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Writing files so that a crash or a full disk never destroys what was there before.

The new content goes to a temporary file beside the target, which replaces the target only
once all of it is written.
"""

from __future__ import annotations

import contextlib
import errno
import os
import shutil
import tempfile
import threading
from typing import IO, Iterator, Optional, Union


@contextlib.contextmanager
def atomic_open(path: str, mode: str = "w", encoding: Optional[str] = "utf-8",
                newline: Optional[str] = None) -> Iterator[IO]:
    """Open a temporary file that becomes ``path`` when the block ends without an error.

    ``mode`` is ``"w"`` (text) or ``"wb"``. The directory is created. When the block raises,
    the temporary file is removed and ``path`` is as it was. As with a plain ``open``: a
    symbolic link stays a link (the file it points to is replaced), the file keeps its
    permissions, and a file that may not be written is not written.
    """
    if "w" not in mode or "+" in mode or "a" in mode:
        raise ValueError("atomic_open writes whole files: mode 'w' or 'wb'")
    path = os.path.realpath(path)  # the file behind a link, not the link
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    exists = os.path.exists(path)
    if exists and not os.access(path, os.W_OK):
        raise PermissionError(errno.EACCES, "The file is write-protected", path)
    options = {} if "b" in mode else {"encoding": encoding, "newline": newline}
    try:
        # a short name of its own (unique per process and thread without making a long file
        # name longer than the file system allows)
        descriptor, temporary = tempfile.mkstemp(prefix=".osl-", suffix=".tmp", dir=directory)
    except PermissionError:
        if not exists:
            raise
        # a folder that takes no new files, but the file itself may be written: in place, as
        # before (not safe against a crash, but better than not saving at all)
        with open(path, mode, **options) as handle:
            yield handle
        return
    try:
        with os.fdopen(descriptor, mode, **options) as handle:
            yield handle
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass  # a file system without fsync
        if exists:
            shutil.copymode(path, temporary)
        else:
            os.chmod(temporary, 0o666 & ~_umask())  # as a new file gets them (mkstemp: owner only)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(temporary)
        raise


def _umask() -> int:
    global _UMASK
    if _UMASK is None:
        # (read once: reading it means setting it for a moment)
        with _UMASK_LOCK:
            if _UMASK is None:
                current = os.umask(0)
                os.umask(current)
                _UMASK = current
    return _UMASK


_UMASK: Optional[int] = None
_UMASK_LOCK = threading.Lock()


def atomic_write(path: str, data: Union[str, bytes], encoding: str = "utf-8",
                 newline: Optional[str] = None) -> None:
    """Write ``data`` (text or bytes) to ``path``, replacing it only when all of it is written."""
    if isinstance(data, bytes):
        with atomic_open(path, "wb") as handle:
            handle.write(data)
    else:
        with atomic_open(path, "w", encoding=encoding, newline=newline) as handle:
            handle.write(data)
