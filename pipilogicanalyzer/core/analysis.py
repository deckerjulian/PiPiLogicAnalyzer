# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Sample analysis helpers.

Everything that used to be a per-sample loop in the C# code lives here as a
vectorised ``numpy`` operation:

* :class:`ChannelTransitions` indexes the edges of a channel once, so rendering,
  tool tips and measurements do O(visible transitions) work instead of
  O(samples);
* :func:`measure_channel` computes the pulse statistics shown by the measure
  dialog.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np


@dataclass(frozen=True)
class Interval:
    """A run of identical samples."""

    start: int
    end: int  # exclusive
    value: bool
    duration: float  # seconds

    @property
    def sample_count(self) -> int:
        return self.end - self.start


class ChannelTransitions:
    """Index of the edges of a single channel.

    ``starts`` holds the first sample of every run, ``values`` the level of that
    run.  Both arrays are computed once per capture and reused by every consumer.
    A capture that is still streaming in grows with :meth:`extend`; an endless
    stream that only keeps its latest samples moves its window with
    :meth:`slide_to`.  Runs are stored at absolute positions (``origin`` is the
    absolute position of sample 0), so neither costs more than the new samples.
    """

    __slots__ = ("sample_count", "frequency", "origin", "_starts", "_values", "_low", "_high")

    def __init__(self, samples: Optional[np.ndarray], frequency: int, origin: int = 0) -> None:
        self.frequency = max(int(frequency), 1)
        self.origin = origin
        self.sample_count = 0
        self._starts = np.zeros(0, dtype=np.int64)
        self._values = np.zeros(0, dtype=np.uint8)
        #: the runs of the window are ``_starts[_low:_high]`` (the first may start before it)
        self._low = self._high = 0
        if samples is not None and samples.size:
            self.extend(samples, int(samples.shape[0]))

    # ----------------------------------------------------------------- arrays
    @property
    def absolute_starts(self) -> np.ndarray:
        return self._starts[self._low : self._high]

    @property
    def starts(self) -> np.ndarray:
        """First sample of every run (the first one clipped to 0)."""
        starts = self.absolute_starts
        if not self.origin and (not len(starts) or starts[0] >= 0):
            return starts
        starts = starts - self.origin
        if len(starts):
            starts[0] = max(starts[0], 0)
        return starts

    @property
    def values(self) -> np.ndarray:
        return self._values[self._low : self._high]

    def __len__(self) -> int:
        return self._high - self._low

    # ---------------------------------------------------------------- growing
    def extend(self, samples: np.ndarray, sample_count: int) -> None:
        """Indexes ``samples`` from :attr:`sample_count` up to ``sample_count``.

        ``samples`` is the whole channel (window) so far; the work is proportional to the new
        samples.
        """
        old = self.sample_count
        if sample_count <= old:
            return
        if old == 0:
            part = np.asarray(samples[:sample_count], dtype=np.uint8)
            changes = np.flatnonzero(part[1:] != part[:-1]) + 1
            starts = np.concatenate(([0], changes)).astype(np.int64) + self.origin
            values = part[np.concatenate(([0], changes))]
            self._low = self._high  # nothing of an earlier window is kept
        else:
            part = np.asarray(samples[old - 1 : sample_count], dtype=np.uint8)
            changes = np.flatnonzero(part[1:] != part[:-1]) + 1
            starts = changes.astype(np.int64) + (old - 1 + self.origin)
            values = part[changes]

        kept = len(self)
        if self._high + len(starts) > len(self._starts):
            capacity = max(2 * (kept + len(starts)), 1024)
            new_starts = np.empty(capacity, dtype=np.int64)
            new_values = np.empty(capacity, dtype=np.uint8)
            new_starts[:kept] = self.absolute_starts
            new_values[:kept] = self.values
            self._starts, self._values = new_starts, new_values
            self._low, self._high = 0, kept
        self._starts[self._high : self._high + len(starts)] = starts
        self._values[self._high : self._high + len(starts)] = values
        self._high += len(starts)
        self.sample_count = sample_count

    def slide_to(self, origin: int) -> None:
        """Drops the samples before the absolute position ``origin`` (moving window)."""
        dropped = origin - self.origin
        if dropped <= 0:
            return
        self.origin = origin
        if dropped >= self.sample_count:
            self.sample_count = 0
            self._low = self._high
            return
        self.sample_count -= dropped
        self._low += max(int(np.searchsorted(self.absolute_starts, origin, side="right")) - 1, 0)

    # ---------------------------------------------------------------- queries
    @property
    def edge_count(self) -> int:
        """Number of level changes in the channel."""
        return max(len(self) - 1, 0)

    def run_index(self, sample: int) -> int:
        """Index of the run containing ``sample`` (-1 when out of range)."""
        if self.sample_count == 0 or sample < 0 or sample >= self.sample_count:
            return -1
        return int(np.searchsorted(self.absolute_starts, sample + self.origin, side="right") - 1)

    def interval_at(self, sample: int) -> Optional[Interval]:
        index = self.run_index(sample)
        if index < 0:
            return None
        return self._interval(index)

    def _interval(self, index: int) -> Interval:
        starts = self.absolute_starts
        start = max(int(starts[index]) - self.origin, 0)
        end = int(starts[index + 1]) - self.origin if index + 1 < len(self) else self.sample_count
        return Interval(
            start=start,
            end=end,
            value=bool(self.values[index]),
            duration=(end - start) / self.frequency,
        )

    def runs_in_range(self, first_sample: int, last_sample: int) -> tuple[np.ndarray, np.ndarray]:
        """Return ``(starts, values)`` of every run overlapping ``[first, last]``.

        The first returned run starts at or before ``first_sample`` so callers can
        draw the level that is already active when the window opens.
        """
        if self.sample_count == 0:
            return self.starts, self.values

        first_sample = max(int(first_sample), 0)
        last_sample = min(int(last_sample), self.sample_count - 1)
        if last_sample < first_sample:
            return self.starts[:0], self.values[:0]

        starts = self.absolute_starts
        begin = max(int(np.searchsorted(starts, first_sample + self.origin, side="right")) - 1, 0)
        end = int(np.searchsorted(starts, last_sample + self.origin, side="right"))
        return starts[begin:end] - self.origin, self.values[begin:end]

    def levels_and_edges(self, boundaries: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """For sample positions ``boundaries``: the level at each (but the last) and the
        number of edges before each, e.g. to draw columns of many samples."""
        starts = self.absolute_starts
        positions = boundaries + self.origin
        run_at_boundary = np.searchsorted(starts, positions[:-1], side="right") - 1
        run_at_boundary = np.clip(run_at_boundary, 0, len(self) - 1)
        # starts[0] is the start of the first run, not an edge.
        edges_before = np.searchsorted(starts[1:], positions, side="left")
        return self.values[run_at_boundary], edges_before


def build_transitions(channels: Sequence, frequency: int, origin: int = 0) -> list[ChannelTransitions]:
    """Index every channel of a capture (``origin``: stream position of its first sample)."""
    return [ChannelTransitions(channel.samples, frequency, origin) for channel in channels]


@dataclass
class PulseMeasures:
    """Statistics of the pulses of one channel over a sample range."""

    channel_name: str = ""
    positive_pulses: int = 0
    negative_pulses: int = 0
    average_positive_duration: float = 0.0
    average_negative_duration: float = 0.0
    predicted_positive_duration: float = 0.0
    predicted_negative_duration: float = 0.0
    average_frequency: float = 0.0
    predicted_frequency: float = 0.0
    duty_cycle: float = 0.0


def _pulse_lengths(samples: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Lengths of the high and low runs of ``samples``."""
    if samples.size == 0:
        empty = np.zeros(0, dtype=np.int64)
        return empty, empty

    changes = np.flatnonzero(samples[1:] != samples[:-1]) + 1
    starts = np.concatenate(([0], changes))
    ends = np.concatenate((changes, [samples.size]))
    lengths = (ends - starts).astype(np.int64)
    values = samples[starts]
    return lengths[values != 0], lengths[values == 0]


def _mode_length(lengths: np.ndarray) -> float:
    """Most representative pulse length.

    The original code sorted the lengths by how often they occurred and averaged
    the last 5%, which produced wildly different values depending on how many
    distinct lengths existed.  The most frequent length (the mode) is both
    cheaper to compute and stable in the presence of outliers; ties fall back to
    the median.
    """
    if lengths.size == 0:
        return 0.0
    values, counts = np.unique(lengths, return_counts=True)
    best = counts.max()
    candidates = values[counts == best]
    if candidates.size == 1:
        return float(candidates[0])
    return float(np.median(lengths))


def measure_channel(
    samples: Optional[np.ndarray], frequency: int, channel_name: str = ""
) -> PulseMeasures:
    """Compute the pulse statistics displayed by the measure dialog."""
    measures = PulseMeasures(channel_name=channel_name)
    if samples is None or samples.size == 0 or frequency <= 0:
        return measures

    samples = np.asarray(samples, dtype=np.uint8)
    sample_period = 1.0 / frequency
    positive, negative = _pulse_lengths(samples)

    measures.positive_pulses = int(positive.size)
    measures.negative_pulses = int(negative.size)
    measures.average_positive_duration = float(positive.mean() * sample_period) if positive.size else 0.0
    measures.average_negative_duration = float(negative.mean() * sample_period) if negative.size else 0.0
    measures.predicted_positive_duration = _mode_length(positive) * sample_period
    measures.predicted_negative_duration = _mode_length(negative) * sample_period

    average_period = measures.average_positive_duration + measures.average_negative_duration
    predicted_period = measures.predicted_positive_duration + measures.predicted_negative_duration
    measures.average_frequency = 1.0 / average_period if average_period > 0 else 0.0
    measures.predicted_frequency = 1.0 / predicted_period if predicted_period > 0 else 0.0

    high_samples = int(samples.sum())
    measures.duty_cycle = high_samples / samples.size * 100.0
    return measures
