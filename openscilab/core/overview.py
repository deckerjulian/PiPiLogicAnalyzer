# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Large captures: min/max overviews and progressive transfer.

An :class:`Overview` is a pyramid of minima and maxima over blocks of samples (each level
``FACTOR`` times coarser). The display of millions of samples takes the envelope of a pixel
column from the level that fits, so it costs the width of the screen, not the length of the
capture.

A :class:`ProgressiveCapture` is a capture that arrives in parts (an oscilloscope over the
network): the overviews come first, then *tiles* of full resolution, the visible range first
(:meth:`prioritize`). The samples live in memory-mapped arrays; :attr:`loaded` says which tiles
arrived. Decoding, export and saving wait until :attr:`complete`.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

#: Samples per block of the first level, and from level to level
FACTOR = 16
#: Default tile size of a progressive transfer (samples)
TILE_SAMPLES = 1 << 18


class Overview:
    """Minima and maxima of ``values`` over blocks of ``FACTOR``, ``FACTOR²``, ... samples."""

    def __init__(self, levels: list[tuple[np.ndarray, np.ndarray]], length: int, factor: int = FACTOR,
                 base: Optional[int] = None) -> None:
        #: (mins, maxs) per level; level k covers blocks of base * factor ** k samples
        self.levels = levels
        self.length = int(length)
        self.factor = int(factor)
        self.base = int(base if base is not None else factor)

    @staticmethod
    def build(values: np.ndarray, factor: int = FACTOR) -> "Overview":
        values = np.asarray(values)
        return Overview.from_blocks(*_reduce(values, values, factor), len(values), factor, factor)

    @staticmethod
    def from_blocks(mins: np.ndarray, maxs: np.ndarray, length: int, base: int, factor: int = FACTOR) -> "Overview":
        """An overview from the minima and maxima of blocks of ``base`` samples (the coarser
        levels are computed from them)."""
        levels = [(np.asarray(mins), np.asarray(maxs))]
        while len(levels[-1][0]) > 1:
            levels.append(_reduce(*levels[-1], factor))
        return Overview(levels, length, factor, base)

    def block(self, level: int) -> int:
        return self.base * self.factor ** level

    def envelope(self, first: float, last: float, columns: int) -> tuple[np.ndarray, np.ndarray]:
        """Minimum and maximum of each of ``columns`` equal parts of ``[first, last)``."""
        columns = max(int(columns), 1)
        if not self.levels or self.length == 0:
            return np.zeros(columns), np.zeros(columns)
        span = max(last - first, 1.0)
        per_column = span / columns
        # A level with at least 16 blocks per column: the envelope is then at most two blocks
        # wider than the exact one, and costs the number of columns times the blocks per column.
        level = 0
        while level + 1 < len(self.levels) and self.block(level + 1) * 16 <= per_column:
            level += 1
        size = self.block(level)
        mins, maxs = self.levels[level]
        edges = first + np.arange(columns + 1) * per_column
        start = np.clip((edges[:-1] // size).astype(np.int64), 0, len(mins) - 1)
        stop = np.clip(np.maximum((np.ceil(edges[1:] / size)).astype(np.int64), start + 1), 1, len(mins))
        # Columns spanning several blocks: reduce over the blocks between start and stop (the
        # arrays end at the last stop, so the last column does not reach beyond the range).
        end = int(stop[-1])
        low = np.minimum.reduceat(mins[:end], start)
        high = np.maximum.reduceat(maxs[:end], start)
        # reduceat reduces up to the next column's first block; the block a column shares with the
        # next one (its partial last block) is added, and columns inside one block take its value.
        single = stop - start <= 1
        low = np.where(single, mins[start], np.minimum(low, mins[stop - 1]))
        high = np.where(single, maxs[start], np.maximum(high, maxs[stop - 1]))
        return low, high


def _reduce(mins: np.ndarray, maxs: np.ndarray, factor: int) -> tuple[np.ndarray, np.ndarray]:
    count = (len(mins) + factor - 1) // factor
    pad = count * factor - len(mins)
    if pad:
        mins = np.concatenate([mins, np.repeat(mins[-1:], pad)])
        maxs = np.concatenate([maxs, np.repeat(maxs[-1:], pad)])
    return mins.reshape(count, factor).min(axis=1), maxs.reshape(count, factor).max(axis=1)


def column_envelope(values: np.ndarray, columns: int) -> tuple[np.ndarray, np.ndarray]:
    """Minimum and maximum of ``columns`` equal parts of ``values`` (loaded samples)."""
    count = len(values)
    columns = max(min(int(columns), count), 1)
    if count == 0:
        return np.zeros(columns), np.zeros(columns)
    starts = (np.arange(columns) * count) // columns
    return np.minimum.reduceat(values, starts), np.maximum.reduceat(values, starts)


@dataclass
class ProgressiveCapture:
    """Book-keeping of a capture that arrives tile by tile.

    The arrays are the samples of the session's channels (memory-mapped, filled by the driver);
    ``overviews`` holds the overview of each channel by key (``("d", channel number)`` for digital,
    ``("a", analog channel number)`` for analog channels).
    """

    length: int
    tile_samples: int = TILE_SAMPLES
    overviews: dict[tuple[str, int], Overview] = field(default_factory=dict)
    loaded: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=bool))
    #: tiles the display wants first: (first tile, last tile)
    priority: Optional[tuple[int, int]] = None
    #: the transfer stopped before all tiles arrived (connection lost, aborted); resume fetches the rest
    interrupted: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _listeners: list[Callable[[], None]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if len(self.loaded) != self.tile_count:
            self.loaded = np.zeros(self.tile_count, dtype=bool)

    @property
    def tile_count(self) -> int:
        return (self.length + self.tile_samples - 1) // self.tile_samples

    @property
    def complete(self) -> bool:
        return bool(self.loaded.all()) if self.tile_count else True

    @property
    def fraction(self) -> float:
        return float(self.loaded.mean()) if self.tile_count else 1.0

    def tile_range(self, tile: int) -> tuple[int, int]:
        start = tile * self.tile_samples
        return start, min(start + self.tile_samples, self.length)

    def tiles_of(self, first: int, last: int) -> range:
        first = max(int(first), 0)
        last = min(int(last), self.length)
        if last <= first:
            return range(0)
        return range(first // self.tile_samples, (last - 1) // self.tile_samples + 1)

    def is_loaded(self, first: int, last: int) -> bool:
        tiles = self.tiles_of(first, last)
        return bool(self.loaded[tiles.start:tiles.stop].all()) if len(tiles) else True

    def missing(self) -> list[int]:
        return [int(tile) for tile in np.flatnonzero(~self.loaded)]

    def next_tile(self) -> Optional[int]:
        """The tile to fetch next: a missing one of the prioritized range first, then in order."""
        with self._lock:
            if self.priority is not None:
                first, last = self.priority
                for tile in range(max(first, 0), min(last, self.tile_count - 1) + 1):
                    if not self.loaded[tile]:
                        return tile
            missing = np.flatnonzero(~self.loaded)
            return int(missing[0]) if len(missing) else None

    def prioritize(self, first: int, last: int) -> None:
        """The display shows ``[first, last)``: fetch its tiles first."""
        tiles = self.tiles_of(first, last)
        with self._lock:
            self.priority = (tiles.start, tiles.stop - 1) if len(tiles) else None

    def mark_loaded(self, tile: int) -> None:
        with self._lock:
            self.loaded[tile] = True
            listeners = list(self._listeners)
        for listener in listeners:
            listener()

    def subscribe(self, listener: Callable[[], None]) -> None:
        with self._lock:
            self._listeners.append(listener)

    def loaded_ranges(self) -> list[tuple[int, int]]:
        """Sample ranges that arrived, merged."""
        ranges: list[tuple[int, int]] = []
        for tile in np.flatnonzero(self.loaded):
            start, end = self.tile_range(int(tile))
            if ranges and ranges[-1][1] == start:
                ranges[-1] = (ranges[-1][0], end)
            else:
                ranges.append((start, end))
        return ranges
