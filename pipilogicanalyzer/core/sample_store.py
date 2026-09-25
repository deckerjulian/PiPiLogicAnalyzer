# Copyright (C) 2026 Julian Decker
#
# Part of PiPiLogicAnalyzer.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The samples of a streaming capture while they arrive, one ``uint8`` array per channel.

:class:`SampleStore` keeps everything, :class:`RingStore` only the latest samples of an endless
stream. Both hand out plain arrays (views), which the display and the decoders need.
"""

from __future__ import annotations

from typing import Sequence

import numpy as np


class SampleStore:
    """All samples of a capture, into arrays allocated for ``capacity`` samples."""

    def __init__(self, channels: Sequence[int], capacity: int) -> None:
        self.channels = tuple(channels)
        self.arrays = {channel: np.zeros(capacity, dtype=np.uint8) for channel in self.channels}
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

    def __init__(self, channels: Sequence[int], keep: int) -> None:
        super().__init__(channels, 2 * keep)
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
        return {channel: view.copy() for channel, view in views.items()}, first
