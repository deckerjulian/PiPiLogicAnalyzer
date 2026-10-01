# Copyright (C) 2026 Julian Decker
#
# Part of PiPiLogicAnalyzer.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The samples of a streaming capture while they arrive, one ``uint8`` array per channel.

:class:`SampleStore` keeps everything, :class:`RingStore` only the latest samples of an endless
stream. Both hand out plain arrays (views), which the display and the decoders need.

With a :class:`DiskAllocator` the arrays are memory-mapped files instead of memory: the operating
system keeps only the parts in use in RAM, so a stream is limited by the free disk space.
"""

from __future__ import annotations

import glob
import logging
import os
import shutil
import tempfile
from typing import Optional, Sequence

import numpy as np

log = logging.getLogger("pipilogicanalyzer.core")

#: Disk space left free when a capture is recorded to disk
DISK_RESERVE_BYTES = 4 << 30
#: Copies between memory-mapped arrays go in blocks of this many samples
COPY_BLOCK = 64 << 20


class MemoryAllocator:
    """Arrays in memory."""

    on_disk = False

    def zeros(self, count: int, dtype=np.uint8) -> np.ndarray:
        return np.zeros(count, dtype=dtype)


class DiskAllocator:
    """Arrays in memory-mapped files of :func:`disk_directory`.

    The files are unlinked right after mapping (POSIX), so their space is released with the arrays,
    also after a crash; where that fails (Windows) :func:`clean_disk_directory` removes them later.
    """

    on_disk = True

    def __init__(self, directory: Optional[str] = None) -> None:
        self.directory = directory or disk_directory()

    def zeros(self, count: int, dtype=np.uint8) -> np.ndarray:
        if count <= 0:
            return np.zeros(0, dtype=dtype)
        os.makedirs(self.directory, exist_ok=True)
        handle, path = tempfile.mkstemp(prefix="stream-", suffix=".bin", dir=self.directory)
        os.close(handle)
        array = np.memmap(path, dtype=dtype, mode="w+", shape=(count,))
        try:
            os.unlink(path)
        except OSError:  # Windows: the mapped file stays until the next start
            pass
        return array


def disk_directory() -> str:
    return os.path.join(tempfile.gettempdir(), "pipilogicanalyzer-streams")


def clean_disk_directory(directory: Optional[str] = None) -> None:
    """Removes the files of earlier runs that could not be unlinked while mapped."""
    for path in glob.glob(os.path.join(directory or disk_directory(), "stream-*.bin")):
        try:
            os.unlink(path)
        except OSError:
            pass


def disk_sample_bytes(directory: Optional[str] = None) -> int:
    """Bytes a capture recorded to disk may use: the free space less a reserve."""
    directory = directory or disk_directory()
    probe = directory if os.path.isdir(directory) else os.path.dirname(directory)
    try:
        free = shutil.disk_usage(probe).free
    except OSError as error:
        log.debug("Free disk space unknown: %s", error)
        return 0
    return max(free - DISK_RESERVE_BYTES, 0)


def is_on_disk(array: Optional[np.ndarray]) -> bool:
    return isinstance(array, np.memmap) or isinstance(getattr(array, "base", None), np.memmap)


def copy_array(values: np.ndarray, allocator: "MemoryAllocator | DiskAllocator") -> np.ndarray:
    """A copy of ``values`` from ``allocator``, block by block (no second copy in memory)."""
    if not allocator.on_disk:
        return np.array(values, copy=True)
    result = allocator.zeros(len(values), values.dtype)
    for start in range(0, len(values), COPY_BLOCK):
        result[start : start + COPY_BLOCK] = values[start : start + COPY_BLOCK]
    return result


class SampleStore:
    """All samples of a capture, into arrays allocated for ``capacity`` samples."""

    def __init__(
        self,
        channels: Sequence[int],
        capacity: int,
        allocator: "MemoryAllocator | DiskAllocator | None" = None,
    ) -> None:
        self.channels = tuple(channels)
        self.allocator = allocator or MemoryAllocator()
        self.arrays = {channel: self.allocator.zeros(capacity) for channel in self.channels}
        #: samples received per channel
        self.total = 0

    @property
    def capacity(self) -> int:
        return len(next(iter(self.arrays.values()))) if self.arrays else 0

    def append(self, samples: dict[int, np.ndarray]) -> None:
        """Adds the same number of samples to every channel; what exceeds the capacity is dropped."""
        count = min(len(next(iter(samples.values()))), self.capacity - self.total) if samples else 0
        for channel, values in samples.items():
            self.arrays[channel][self.total : self.total + count] = values[:count]
        self.total += count

    def window(self) -> tuple[dict[int, np.ndarray], int]:
        """Views of the samples kept, and the stream position of their first sample."""
        return {channel: array[: self.total] for channel, array in self.arrays.items()}, 0

    def result(self) -> tuple[dict[int, np.ndarray], int]:
        return self.window()


class RingStore(SampleStore):
    """The latest ``keep`` samples of an endless stream.

    Every sample is written twice, at ``i`` and ``i + keep`` of arrays twice as long, so the
    window is always one contiguous view.
    """

    def __init__(
        self,
        channels: Sequence[int],
        keep: int,
        allocator: "MemoryAllocator | DiskAllocator | None" = None,
    ) -> None:
        super().__init__(channels, 2 * keep, allocator)
        self.keep = keep

    def append(self, samples: dict[int, np.ndarray]) -> None:
        count = len(next(iter(samples.values()))) if samples else 0
        skip = max(count - self.keep, 0)  # more than the window at once: only its end matters
        position = (self.total + skip) % self.keep
        first = min(count - skip, self.keep - position)
        for channel, values in samples.items():
            array = self.arrays[channel]
            for offset in (0, self.keep):
                array[offset + position : offset + position + first] = values[skip : skip + first]
                array[offset : offset + count - skip - first] = values[skip + first :]
        self.total += count

    def window(self) -> tuple[dict[int, np.ndarray], int]:
        if self.total <= self.keep:
            return super().window()
        start = self.total % self.keep
        views = {channel: array[start : start + self.keep] for channel, array in self.arrays.items()}
        return views, self.total - self.keep

    def result(self) -> tuple[dict[int, np.ndarray], int]:
        # A copy: the arrays of twice the window are released with the store.
        views, first = self.window()
        return {channel: copy_array(view, self.allocator) for channel, view in views.items()}, first
