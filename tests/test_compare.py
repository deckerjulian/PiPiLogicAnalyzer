"""Comparing a capture with a reference capture."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time

import numpy as np

from openscilab.core import compare
from openscilab.core.compare import align_offset, compare_sessions, difference_regions
from openscilab.driver.models import AnalyzerChannel, CaptureSession


def session_of(channels: dict[int, np.ndarray]) -> CaptureSession:
    return CaptureSession(
        capture_channels=[
            AnalyzerChannel(channel_number=n, samples=np.asarray(s, dtype=np.uint8)) for n, s in channels.items()
        ]
    )


def test_identical():
    data = np.array([0, 1, 1, 0, 1], dtype=np.uint8)
    result = compare_sessions(session_of({0: data}), session_of({0: data.copy()}))
    assert result.identical
    assert result.differences == {0: []}
    assert result.compared_samples == 5


def test_differences_per_channel():
    reference = session_of({0: [0, 0, 0, 0, 0, 0], 1: [1, 1, 1, 1, 1, 1], 2: [0] * 6})
    capture = session_of({0: [0, 1, 1, 0, 0, 1], 1: [1, 1, 1, 1, 0, 1], 3: [1] * 6})
    result = compare_sessions(reference, capture)
    # channel 3 has no reference
    assert result.differences == {0: [(1, 3), (5, 6)], 1: [(4, 5)]}
    assert result.differing_samples == 4
    assert result.difference_count == 3


def test_tolerance():
    reference = session_of({0: [0, 0, 0, 0, 0, 0, 0, 0]})
    capture = session_of({0: [0, 1, 0, 0, 1, 1, 1, 0]})
    assert compare_sessions(reference, capture, tolerance=1).differences == {0: [(4, 7)]}
    assert compare_sessions(reference, capture, tolerance=3).identical


def test_offset_and_map():
    base = np.array([0, 0, 1, 1, 0, 1, 0, 0, 1, 0], dtype=np.uint8)
    reference = session_of({5: base})
    # the capture lags two samples behind: capture[i] == reference[i - 2]
    shifted = np.concatenate(([0, 0], base[:-2]))
    capture = session_of({0: shifted})
    assert compare_sessions(reference, capture).differences == {}
    result = compare_sessions(reference, capture, offset=-2, channel_map={0: 5})
    assert result.identical
    assert result.compared_samples == 8
    assert not compare_sessions(reference, capture, offset=0, channel_map={0: 5}).identical


def test_blocks_join(monkeypatch):
    monkeypatch.setattr(compare, "COMPARE_BLOCK", 3)
    reference = session_of({0: [0] * 10})
    capture = session_of({0: [0, 1, 1, 1, 1, 1, 0, 0, 1, 1]})
    assert compare_sessions(reference, capture).differences == {0: [(1, 6), (8, 10)]}


def test_align_offset():
    rng = np.random.default_rng(4)
    base = np.repeat(rng.integers(0, 2, 2000).astype(np.uint8), rng.integers(3, 30, 2000))
    for lag in (0, 7, -13, 40):
        capture = np.roll(base, lag)  # capture[i] == base[i - lag]
        offset = align_offset(session_of({0: base, 1: 1 - base}), session_of({0: capture, 1: 1 - capture}), 50)
        assert offset == -lag
        assert compare_sessions(session_of({0: base}), session_of({0: capture}), offset=offset).differing_samples <= 2 * abs(lag)


def test_align_offset_without_edges():
    flat = np.zeros(100, dtype=np.uint8)
    assert align_offset(session_of({0: flat}), session_of({0: flat}), 10) == 0
    assert align_offset(session_of({0: flat}), session_of({1: flat}), 10) == 0


def test_difference_regions():
    reference = session_of({0: [0] * 12, 1: [0] * 12})
    capture = session_of({0: [0, 1, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0], 1: [0, 0, 1, 1, 1, 0, 0, 0, 0, 0, 1, 1]})
    regions = difference_regions(compare_sessions(reference, capture))
    assert [(r.first_sample, r.last_sample, r.region_name) for r in regions] == [
        (1, 5, "Diff 1"),
        (8, 9, "Diff 2"),
        (10, 12, "Diff 3"),
    ]
    assert regions[0].region_color[0] > regions[0].region_color[1]  # red, green, blue, alpha
    assert difference_regions(compare_sessions(reference, reference)) == []


def test_large_capture_is_fast():
    count = 10_000_000
    rng = np.random.default_rng(5)
    base = np.repeat(rng.integers(0, 2, count // 50).astype(np.uint8), 50)
    capture = np.roll(base, 3)
    capture[5_000_000:5_000_010] ^= 1
    reference_session, capture_session = session_of({0: base}), session_of({0: capture})
    begin = time.perf_counter()
    offset = align_offset(reference_session, capture_session, 100)
    result = compare_sessions(reference_session, capture_session, offset=offset, tolerance=0)
    elapsed = time.perf_counter() - begin
    assert offset == -3
    assert result.differing_samples == 10
    assert elapsed < 3.0
