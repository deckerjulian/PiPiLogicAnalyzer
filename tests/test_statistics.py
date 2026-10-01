"""Channel statistics, setup/hold times and histogram data."""

from __future__ import annotations

import time

import numpy as np
import pytest

from pipilogicanalyzer.core.statistics import (
    bus_value_counts,
    channel_statistics,
    histogram,
    pulse_widths,
    setup_hold,
)
from pipilogicanalyzer.driver.models import EdgeKind


def wave(*runs: tuple[int, int]) -> np.ndarray:
    """Samples from ``(level, length)`` runs."""
    return np.concatenate([np.full(length, level, dtype=np.uint8) for level, length in runs])


def test_square_wave():
    # partial low, then 3 high/low periods of 4 high + 6 low, partial high at the end
    samples = wave((0, 3), (1, 4), (0, 6), (1, 4), (0, 6), (1, 4), (0, 6), (1, 2))
    stats = channel_statistics(samples, 1000)
    assert stats.sample_count == len(samples)
    assert stats.rising_edges == 4
    assert stats.falling_edges == 3
    assert stats.period_count == 3
    assert stats.period_min == stats.period_max == pytest.approx(0.010)
    assert stats.period_std == pytest.approx(0.0)
    assert stats.frequency == pytest.approx(100.0)
    assert stats.high_count == 3 and stats.low_count == 3
    assert stats.high_mean == pytest.approx(0.004)
    assert stats.low_mean == pytest.approx(0.006)
    assert stats.duty_cycle == pytest.approx(40.0)


def test_varying_pulses():
    samples = wave((1, 5), (0, 2), (1, 3), (0, 4), (1, 1), (0, 6), (1, 9))
    stats = channel_statistics(samples, 1)
    # complete: low 2, high 3, low 4, high 1, low 6
    assert stats.high_min == 1 and stats.high_max == 3
    assert stats.low_min == 2 and stats.low_max == 6
    assert stats.low_mean == pytest.approx(4.0)
    assert stats.high_std == pytest.approx(1.0)
    # rising edges at 7, 14, 21: periods 7, 7
    assert stats.period_mean == pytest.approx(7.0)
    assert stats.duty_cycle == pytest.approx((3 / 7 + 1 / 7) / 2 * 100)


def test_range_and_not_enough_edges():
    samples = wave((0, 10), (1, 10), (0, 10))
    stats = channel_statistics(samples, 1)
    assert stats.rising_edges == 1 and stats.falling_edges == 1
    assert stats.high_mean == 10
    assert stats.low_mean is None
    assert stats.period_mean is None and stats.frequency is None and stats.duty_cycle is None
    # inside the high pulse only: no edges
    stats = channel_statistics(samples, 1, start=12, end=18)
    assert stats.sample_count == 6 and stats.rising_edges == 0 and stats.high_mean is None
    # the range cuts the pulse: it is partial
    stats = channel_statistics(samples, 1, start=15)
    assert stats.falling_edges == 1 and stats.high_mean is None
    assert channel_statistics(samples[:0], 1).sample_count == 0


def test_pulse_widths():
    samples = wave((1, 5), (0, 2), (1, 3), (0, 4), (1, 1), (0, 6), (1, 9))
    assert pulse_widths(samples, 2, 1).tolist() == [1.5, 0.5]
    assert pulse_widths(samples, 2, 0).tolist() == [1.0, 2.0, 3.0]
    assert len(pulse_widths(samples[:1], 2, 1)) == 0


def test_setup_hold():
    clock = wave((0, 10), (1, 10), (0, 10), (1, 10), (0, 10), (1, 10))  # rising at 10, 30, 50
    data = wave((0, 7), (1, 25), (0, 20), (1, 8))  # changes at 7, 32, 52
    result = setup_hold(data, clock, 1000)
    assert result.clock_edges == 3
    # setup: 10-7=3, 30-7=23, 50-32=18; hold: 32-10=22, 32-30=2, 52-50=2
    assert result.setup_min == pytest.approx(0.003)
    assert result.worst_setup_sample == 10
    assert result.setup_mean == pytest.approx((3 + 23 + 18) / 3 / 1000)
    assert result.hold_min == pytest.approx(0.002)
    assert result.worst_hold_sample == 30
    assert result.hold_mean == pytest.approx((22 + 2 + 2) / 3 / 1000)

    falling = setup_hold(data, clock, 1000, edge=EdgeKind.FALLING)  # falling at 20, 40
    assert falling.clock_edges == 2
    assert falling.setup_min == pytest.approx(0.008)  # 40 - 32
    assert falling.hold_min == pytest.approx(0.012)  # 52 - 40


def test_setup_hold_range_and_missing_changes():
    clock = wave((0, 10), (1, 10), (0, 10), (1, 10))
    data = wave((0, 25), (1, 15))  # one change at 25
    result = setup_hold(data, clock, 1, start=5)
    assert result.clock_edges == 2
    assert result.setup_count == 1 and result.setup_min == 5 and result.worst_setup_sample == 30
    assert result.hold_count == 1 and result.hold_min == 15 and result.worst_hold_sample == 10
    assert setup_hold(data, np.zeros(40, np.uint8), 1).setup_min is None


def test_setup_hold_bus_data():
    clock = wave((0, 4), (1, 4), (0, 4), (1, 4))
    data = np.array([0] * 6 + [3] * 4 + [2] * 6, dtype=np.uint16)
    result = setup_hold(data, clock, 1)
    assert result.setup_min == 2  # edge at 12, change at 10
    assert result.hold_min == 2  # edge at 4, change at 6


def test_histogram_and_counts():
    counts, edges = histogram(np.array([1.0, 2.0, 2.0, 3.0]), bins=2)
    assert counts.tolist() == [1, 3] and len(edges) == 3
    counts, edges = histogram(np.zeros(0), bins=5)
    assert counts.tolist() == [0] * 5 and len(edges) == 6
    values, counts = bus_value_counts(np.array([3, 1, 3, 3, 200], dtype=np.uint8))
    assert values.tolist() == [1, 3, 200] and counts.tolist() == [1, 3, 1]
    assert values.dtype == np.uint8
    values, counts = bus_value_counts(np.array([1 << 40, 1, 1 << 40], dtype=np.uint64))
    assert values.tolist() == [1, 1 << 40] and counts.tolist() == [1, 2]
    assert len(bus_value_counts(np.zeros(0, np.uint16))[0]) == 0


def test_large_capture_is_fast():
    count = 10_000_000
    rng = np.random.default_rng(3)
    lengths = rng.integers(5, 15, count // 5)
    levels = np.arange(len(lengths)) % 2
    samples = np.repeat(levels.astype(np.uint8), lengths)[:count]
    clock = (np.arange(count) // 10 % 2).astype(np.uint8)
    values = np.repeat(rng.integers(0, 1 << 16, count // 10).astype(np.uint16), 10)
    begin = time.perf_counter()
    stats = channel_statistics(samples, 100_000_000)
    widths = pulse_widths(samples, 100_000_000, 1)
    histogram(widths)
    result = setup_hold(samples, clock, 100_000_000)
    bus_value_counts(values)
    elapsed = time.perf_counter() - begin
    assert stats.rising_edges > 100_000 and len(widths) > 100_000
    assert result.clock_edges == count // 20 - 1 or result.clock_edges == count // 20
    assert elapsed < 3.0
