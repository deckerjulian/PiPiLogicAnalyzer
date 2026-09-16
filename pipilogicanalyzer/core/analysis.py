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
    """

    __slots__ = ("starts", "values", "sample_count", "frequency")

    def __init__(self, samples: Optional[np.ndarray], frequency: int) -> None:
        self.frequency = max(int(frequency), 1)
        if samples is None or samples.size == 0:
            self.starts = np.zeros(0, dtype=np.int64)
            self.values = np.zeros(0, dtype=np.uint8)
            self.sample_count = 0
            return

        samples = np.asarray(samples, dtype=np.uint8)
        self.sample_count = int(samples.shape[0])
        changes = np.flatnonzero(samples[1:] != samples[:-1]) + 1
        self.starts = np.concatenate(([0], changes)).astype(np.int64)
        self.values = samples[self.starts]

    def __len__(self) -> int:
        return int(self.starts.shape[0])

    @property
    def edge_count(self) -> int:
        """Number of level changes in the channel."""
        return max(len(self) - 1, 0)

    def run_index(self, sample: int) -> int:
        """Index of the run containing ``sample`` (-1 when out of range)."""
        if self.sample_count == 0 or sample < 0 or sample >= self.sample_count:
            return -1
        return int(np.searchsorted(self.starts, sample, side="right") - 1)

    def interval_at(self, sample: int) -> Optional[Interval]:
        index = self.run_index(sample)
        if index < 0:
            return None
        return self._interval(index)

    def _interval(self, index: int) -> Interval:
        start = int(self.starts[index])
        end = int(self.starts[index + 1]) if index + 1 < len(self) else self.sample_count
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

        begin = max(int(np.searchsorted(self.starts, first_sample, side="right")) - 1, 0)
        end = int(np.searchsorted(self.starts, last_sample, side="right"))
        return self.starts[begin:end], self.values[begin:end]


def build_transitions(channels: Sequence, frequency: int) -> list[ChannelTransitions]:
    """Index every channel of a capture."""
    return [ChannelTransitions(channel.samples, frequency) for channel in channels]


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
