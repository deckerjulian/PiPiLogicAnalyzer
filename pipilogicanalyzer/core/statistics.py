# Copyright (C) 2026 Julian Decker
#
# Part of PiPiLogicAnalyzer.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Timing statistics of channels: periods, pulse widths, duty cycle, setup and
hold times, and the data of histograms.

Only complete pulses count: the run cut by the start of the range and the one
cut by its end have unknown widths. Everything works on the edges of a channel
(:class:`~.analysis.ChannelTransitions`), never on single samples in Python.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from ..driver.models import EdgeKind
from .analysis import ChannelTransitions


@dataclass
class ChannelStatistics:
    """Statistics of one channel over a sample range (times in seconds)."""

    sample_count: int = 0
    rising_edges: int = 0
    falling_edges: int = 0
    #: Rising edge to rising edge
    period_min: Optional[float] = None
    period_max: Optional[float] = None
    period_mean: Optional[float] = None
    period_std: Optional[float] = None
    #: ``1 / period_mean`` in Hz
    frequency: Optional[float] = None
    high_min: Optional[float] = None
    high_max: Optional[float] = None
    high_mean: Optional[float] = None
    high_std: Optional[float] = None
    low_min: Optional[float] = None
    low_max: Optional[float] = None
    low_mean: Optional[float] = None
    low_std: Optional[float] = None
    #: Mean high share of the complete periods, in percent
    duty_cycle: Optional[float] = None
    high_count: int = 0
    low_count: int = 0
    period_count: int = 0


def _edges(samples: np.ndarray, frequency: int = 1) -> tuple[np.ndarray, np.ndarray]:
    """Positions (first sample of the new level) and new levels of the edges of a channel."""
    transitions = ChannelTransitions(samples, frequency)
    return np.asarray(transitions.starts[1:]), np.asarray(transitions.values[1:])


def _range(samples: np.ndarray, start: int, end: Optional[int]) -> np.ndarray:
    count = len(samples)
    start = min(max(int(start), 0), count)
    end = count if end is None else min(max(int(end), start), count)
    return samples[start:end]


def _summary(lengths: np.ndarray, frequency: int) -> tuple[Optional[float], ...]:
    """min, max, mean, std of ``lengths`` samples in seconds."""
    if lengths.size == 0:
        return None, None, None, None
    seconds = lengths / float(frequency)
    return float(seconds.min()), float(seconds.max()), float(seconds.mean()), float(seconds.std())


def _complete_pulses(positions: np.ndarray, levels: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Lengths and levels of the runs between two edges."""
    return np.diff(positions), levels[:-1]


def channel_statistics(
    samples: np.ndarray, frequency: int, start: int = 0, end: Optional[int] = None
) -> ChannelStatistics:
    """Edge counts, period, pulse width and duty cycle statistics of ``samples[start:end]``."""
    frequency = max(int(frequency), 1)
    window = _range(samples, start, end)
    stats = ChannelStatistics(sample_count=len(window))
    if len(window) < 2:
        return stats
    positions, levels = _edges(window, frequency)
    rising = levels != 0
    stats.rising_edges = int(np.count_nonzero(rising))
    stats.falling_edges = len(levels) - stats.rising_edges

    lengths, pulse_levels = _complete_pulses(positions, levels)
    high = lengths[pulse_levels != 0]
    low = lengths[pulse_levels == 0]
    stats.high_count, stats.low_count = len(high), len(low)
    stats.high_min, stats.high_max, stats.high_mean, stats.high_std = _summary(high, frequency)
    stats.low_min, stats.low_max, stats.low_mean, stats.low_std = _summary(low, frequency)

    rising_index = np.flatnonzero(rising)
    periods = np.diff(positions[rising_index])
    stats.period_count = len(periods)
    stats.period_min, stats.period_max, stats.period_mean, stats.period_std = _summary(periods, frequency)
    if stats.period_mean:
        stats.frequency = 1.0 / stats.period_mean
        # Edges alternate, so the edge after a rising edge followed by another one is falling.
        first = rising_index[:-1]
        high_parts = positions[first + 1] - positions[first]
        stats.duty_cycle = float(np.mean(high_parts / periods) * 100.0)
    return stats


def pulse_widths(samples: np.ndarray, frequency: int, level: int) -> np.ndarray:
    """Widths in seconds of the complete pulses of ``level`` (1: high, 0: low)."""
    frequency = max(int(frequency), 1)
    if len(samples) < 2:
        return np.zeros(0, dtype=np.float64)
    positions, levels = _edges(samples, frequency)
    lengths, pulse_levels = _complete_pulses(positions, levels)
    selected = lengths[(pulse_levels != 0) == bool(level)]
    return selected / float(frequency)


@dataclass
class SetupHold:
    """Setup and hold times of a data signal around the edges of a clock (seconds).

    The worst samples are the clock edges with the shortest time; ``None`` where no
    clock edge has a data change before (setup) or after it (hold).
    """

    clock_edges: int = 0
    setup_count: int = 0
    hold_count: int = 0
    setup_min: Optional[float] = None
    setup_mean: Optional[float] = None
    hold_min: Optional[float] = None
    hold_mean: Optional[float] = None
    worst_setup_sample: Optional[int] = None
    worst_hold_sample: Optional[int] = None


def _value_changes(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values)
    return (np.flatnonzero(values[1:] != values[:-1]) + 1).astype(np.int64)


def clock_edge_positions(clock: np.ndarray, edge: EdgeKind = EdgeKind.RISING) -> np.ndarray:
    """First samples at the new level of the ``edge`` edges of ``clock``."""
    if len(clock) < 2:
        return np.zeros(0, dtype=np.int64)
    positions, levels = _edges(clock)
    if edge == EdgeKind.RISING:
        positions = positions[levels != 0]
    elif edge == EdgeKind.FALLING:
        positions = positions[levels == 0]
    return positions.astype(np.int64)


def setup_hold(
    data: np.ndarray,
    clock: np.ndarray,
    frequency: int,
    edge: EdgeKind = EdgeKind.RISING,
    start: int = 0,
    end: Optional[int] = None,
) -> SetupHold:
    """Setup (last data change at or before a clock edge → edge) and hold (edge → next
    data change after it) times over ``[start, end)``.

    ``data`` is one channel or any value array (e.g. :func:`~.buses.bus_values`),
    where every change of value counts. Sample positions are relative to the capture.
    """
    frequency = max(int(frequency), 1)
    first = min(max(int(start), 0), len(clock))
    data = _range(data, start, end)
    clock = _range(clock, start, end)
    result = SetupHold()
    edges = clock_edge_positions(clock, edge)
    result.clock_edges = len(edges)
    if not len(edges):
        return result
    changes = _value_changes(data)

    before = np.searchsorted(changes, edges, side="right") - 1
    has_setup = before >= 0
    setup = edges[has_setup] - changes[before[has_setup]]
    if len(setup):
        worst = int(np.argmin(setup))
        result.setup_count = len(setup)
        result.setup_min = float(setup[worst]) / frequency
        result.setup_mean = float(setup.mean()) / frequency
        result.worst_setup_sample = int(edges[has_setup][worst]) + first

    after = np.searchsorted(changes, edges, side="right")
    has_hold = after < len(changes)
    hold = changes[after[has_hold]] - edges[has_hold]
    if len(hold):
        worst = int(np.argmin(hold))
        result.hold_count = len(hold)
        result.hold_min = float(hold[worst]) / frequency
        result.hold_mean = float(hold.mean()) / frequency
        result.worst_hold_sample = int(edges[has_hold][worst]) + first
    return result


# -------------------------------------------------------------- histograms
def histogram(values: np.ndarray, bins: int = 50) -> tuple[np.ndarray, np.ndarray]:
    """``(counts, edges)`` of ``values`` in ``bins`` equal bins (``bins + 1`` edges)."""
    values = np.asarray(values)
    if values.size == 0:
        return np.zeros(bins, dtype=np.int64), np.linspace(0.0, 1.0, bins + 1)
    return np.histogram(values, bins=bins)


def bus_value_counts(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """``(values, counts)`` of the distinct values of a bus, sorted by value."""
    values = np.asarray(values)
    if values.size == 0:
        return values[:0], np.zeros(0, dtype=np.int64)
    if values.dtype.kind == "u" and values.dtype.itemsize <= 2:
        # Counting beats sorting for narrow buses.
        counts = np.bincount(values, minlength=0)
        present = np.flatnonzero(counts)
        return present.astype(values.dtype), counts[present].astype(np.int64)
    unique, counts = np.unique(values, return_counts=True)
    return unique, counts.astype(np.int64)
