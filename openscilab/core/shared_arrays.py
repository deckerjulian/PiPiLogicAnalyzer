# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Arrays two processes share: the device process writes the samples, the application reads them.

:func:`zeros` makes an array in a *segment* of shared memory (or, ``on_disk``, of a file in the
stream folder, for captures recorded to disk). :func:`dumps` pickles objects with every array of
such a segment as a reference to it (name, offset, shape) instead of its bytes, and copies a large
other array into a new segment first; :func:`loads` maps the segments and gives views of them - the
samples are never copied through the pipe between the processes.

A segment lives as long as an array of it lives in one of the processes: the maps are released with
the last array, the names removed (POSIX) by the process that mapped it in, and by the process that
made it when its arrays are gone. The process that made a segment keeps the segments of a message it
sent until the other side confirms it mapped them (:class:`Sent`), so no name disappears in between.

Plain ``mmap`` and ``_posixshmem`` instead of ``multiprocessing.shared_memory``: its resource
tracker removes segments the other process still needs and warns about the ones it handed over.
Qt free.
"""

from __future__ import annotations

import io
import itertools
import mmap
import os
import pickle
import secrets
import sys
import tempfile
import threading
import weakref
from typing import Any, Optional

import numpy as np

#: other arrays at least this large are copied into a segment instead of being pickled
COPY_THRESHOLD = 1 << 20

_POSIX = sys.platform != "win32"
if _POSIX:
    import _posixshmem

#: segments of this process: id of the map -> (weak reference to the map, description)
_segments: dict[int, tuple[weakref.ref, tuple]] = {}
#: segments mapped in from another process, by name (one map for every array of it)
_mapped: "weakref.WeakValueDictionary[str, mmap.mmap]" = weakref.WeakValueDictionary()
_lock = threading.Lock()
_numbers = itertools.count(1)


def _unlink(kind: str, name: str) -> None:
    try:
        if kind == "memory":
            if _POSIX:
                _posixshmem.shm_unlink(name)
        else:
            os.unlink(name)
    except OSError:
        pass  # gone already (the other process removed it), or Windows: removed with the last map


def _register(mm: mmap.mmap, description: tuple, owned: bool) -> None:
    key = id(mm)
    with _lock:
        _segments[key] = (weakref.ref(mm), description)

    def forget() -> None:
        with _lock:
            _segments.pop(key, None)
        if owned:
            _unlink(description[0], description[1])

    weakref.finalize(mm, forget)


def segment(size: int, on_disk: bool = False, directory: Optional[str] = None) -> np.ndarray:
    """A new segment of ``size`` bytes as a ``uint8`` array (zeros)."""
    size = max(int(size), 1)
    if on_disk:
        from .sample_store import disk_directory

        folder = directory or disk_directory()
        os.makedirs(folder, exist_ok=True)
        handle, path = tempfile.mkstemp(prefix="stream-", suffix=".bin", dir=folder)
        try:
            os.ftruncate(handle, size)
            mm = mmap.mmap(handle, size)
        finally:
            os.close(handle)
        description = ("file", path, size)
    else:
        # short names: macOS allows 31 characters
        name = f"/osl{os.getpid() % 100000}_{next(_numbers)}_{secrets.token_hex(3)}"
        if _POSIX:
            handle = _posixshmem.shm_open(name, os.O_CREAT | os.O_EXCL | os.O_RDWR, mode=0o600)
            try:
                os.ftruncate(handle, size)
                mm = mmap.mmap(handle, size)
            finally:
                os.close(handle)
        else:
            name = name.lstrip("/")
            mm = mmap.mmap(-1, size, tagname=name)
        description = ("memory", name, size)
    _register(mm, description, owned=True)
    return np.frombuffer(mm, dtype=np.uint8)


def zeros(count: int, dtype=np.uint8, on_disk: bool = False, directory: Optional[str] = None) -> np.ndarray:
    """An array of ``count`` zeros in a new segment."""
    dtype = np.dtype(dtype)
    if count <= 0:
        return np.zeros(0, dtype=dtype)
    return segment(count * dtype.itemsize, on_disk, directory).view(dtype)[:count]


def _map_of(array: np.ndarray) -> Optional[mmap.mmap]:
    base: Any = array
    while isinstance(base, np.ndarray):
        base = base.base
    if isinstance(base, memoryview):
        base = base.obj
    return base if isinstance(base, mmap.mmap) else None


def describe(array: np.ndarray) -> Optional[tuple]:
    """``(kind, name, size, offset, shape, strides, dtype)`` of an array in a segment, else ``None``."""
    mm = _map_of(array)
    if mm is None:
        return None
    with _lock:
        entry = _segments.get(id(mm))
    if entry is None or entry[0]() is not mm:
        return None
    kind, name, size = entry[1]
    start = np.frombuffer(mm, dtype=np.uint8, count=1).__array_interface__["data"][0] if size else 0
    offset = array.__array_interface__["data"][0] - start
    return (kind, name, size, int(offset), tuple(array.shape), tuple(array.strides), array.dtype.str)


def is_file_backed(array: Optional[np.ndarray]) -> bool:
    description = describe(array) if isinstance(array, np.ndarray) else None
    return description is not None and description[0] == "file"


def open_array(description: tuple) -> np.ndarray:
    """The array of a description of another process (its segment mapped in once)."""
    kind, name, size, offset, shape, strides, dtype = description
    with _lock:
        mm = _mapped.get(name)
    if mm is None:
        if kind == "file":
            with open(name, "r+b") as handle:
                mm = mmap.mmap(handle.fileno(), size)
        elif _POSIX:
            handle = _posixshmem.shm_open(name, os.O_RDWR, mode=0o600)
            try:
                mm = mmap.mmap(handle, size)
            finally:
                os.close(handle)
        else:
            mm = mmap.mmap(-1, size, tagname=name)
        with _lock:
            _mapped[name] = mm
        # mapped: the name is not needed any more (the memory lives with the maps)
        _unlink(kind, name)
        _register(mm, (kind, name, size), owned=False)
    return np.ndarray(shape, dtype=np.dtype(dtype), buffer=mm, offset=offset, strides=strides)


def clean_segments() -> None:
    """Segments of processes that crashed before they removed them (Linux lists them in /dev/shm;
    elsewhere they go with the last process or the next start of the computer)."""
    folder = "/dev/shm"
    if not os.path.isdir(folder):
        return
    for entry in os.listdir(folder):
        if not entry.startswith("osl") or "_" not in entry:
            continue
        try:
            pid = int(entry[3:].split("_", 1)[0])
            os.kill(pid, 0)  # (pids are shortened to five digits: an alive one keeps its segments)
        except ProcessLookupError:
            _unlink("memory", "/" + entry)
        except (ValueError, PermissionError, OSError):
            continue


# ----------------------------------------------------------------- pickling
class _Pickler(pickle.Pickler):
    def __init__(self, file, keep: list) -> None:
        super().__init__(file, protocol=pickle.HIGHEST_PROTOCOL)
        self.keep = keep

    def persistent_id(self, obj: Any) -> Any:
        if type(obj) is not np.ndarray and not isinstance(obj, np.memmap):
            return None
        description = describe(obj)
        if description is None and obj.nbytes >= COPY_THRESHOLD and obj.dtype.kind in "biuf":
            copy = zeros(obj.size, obj.dtype).reshape(obj.shape)
            copy[...] = obj
            obj, description = copy, describe(copy)
        if description is None:
            return None
        self.keep.append(_map_of(obj))
        return ("array", description)


class _Unpickler(pickle.Unpickler):
    def persistent_load(self, pid: Any) -> Any:
        kind, description = pid
        if kind != "array":
            raise pickle.UnpicklingError(f"unknown reference {kind!r}")
        return open_array(description)


def dumps(obj: Any) -> tuple[bytes, list]:
    """``obj`` pickled with its shared arrays as references; the maps that must live until the other
    process loaded it (see :class:`Sent`)."""
    buffer = io.BytesIO()
    keep: list = []
    _Pickler(buffer, keep).dump(obj)
    return buffer.getvalue(), keep


def loads(data: bytes) -> Any:
    return _Unpickler(io.BytesIO(data)).load()


class Sent:
    """The segments of messages sent to another process, kept until it confirms it mapped them."""

    def __init__(self) -> None:
        self._held: dict[int, list] = {}
        self._lock = threading.Lock()
        self._numbers = itertools.count(1)

    def hold(self, maps: list) -> Optional[int]:
        """Keep ``maps``; the number to confirm (``None``: nothing to keep)."""
        if not maps:
            return None
        number = next(self._numbers)
        with self._lock:
            self._held[number] = maps
        return number

    def release(self, number: int) -> None:
        with self._lock:
            self._held.pop(number, None)

    def clear(self) -> None:
        with self._lock:
            self._held.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._held)
