# Copyright (C) 2026 Julian Decker
#
# Part of PiPiLogicAnalyzer.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Comparing a capture with a reference capture ("golden" capture).

Capture sample ``i`` is compared with reference sample ``i + offset``; the
differences are reported in capture sample positions. :func:`align_offset`
finds the offset that lines both up, from the coincidence of their edges.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from ..driver.models import CaptureSession
from .analysis import ChannelTransitions

#: Samples compared at once (bounds the temporary memory of large captures)
COMPARE_BLOCK = 16 << 20
#: Edge pairs examined by :func:`align_offset` per edge kind and channel
ALIGN_PAIRS = 1 << 21
#: Samples per channel that decide between the best offsets found from the edges
ALIGN_CHECK_SAMPLES = 1 << 22
#: Color of :func:`difference_regions` (RGBA)
DIFFERENCE_COLOR = (230, 70, 70, 90)


@dataclass
class CompareResult:
    """Differences between a capture and its reference."""

    #: capture channel number -> ``(start, end)`` capture sample ranges that differ (end exclusive)
    differences: dict[int, list[tuple[int, int]]] = field(default_factory=dict)
    #: Differing samples summed over the channels
    differing_samples: int = 0
    #: Samples compared per channel (the overlap of both captures)
    compared_samples: int = 0
    offset: int = 0
    tolerance: int = 0

    @property
    def identical(self) -> bool:
        return self.differing_samples == 0

    @property
    def difference_count(self) -> int:
        return sum(len(ranges) for ranges in self.differences.values())


def _pairs(reference: CaptureSession, capture: CaptureSession, channel_map: Optional[dict[int, int]]):
    """``(capture number, capture samples, reference samples)`` of the matched channels."""
    by_number = {c.channel_number: c.samples for c in reference.capture_channels if c.samples is not None}
    for channel in capture.capture_channels:
        if channel.samples is None:
            continue
        number = channel.channel_number
        reference_number = channel_map.get(number, number) if channel_map else number
        samples = by_number.get(reference_number)
        if samples is not None:
            yield number, channel.samples, samples


def _overlap(reference_count: int, capture_count: int, offset: int) -> tuple[int, int]:
    """Capture sample range compared with the reference at ``offset``."""
    first = max(0, -offset)
    last = min(capture_count, reference_count - offset)
    return first, max(last, first)


def _difference_runs(capture: np.ndarray, reference: np.ndarray, first: int, last: int, offset: int):
    """``(starts, ends)`` of the runs of differing samples in ``[first, last)``."""
    all_starts, all_ends = [], []
    for start in range(first, last, COMPARE_BLOCK):
        end = min(start + COMPARE_BLOCK, last)
        differ = np.asarray(capture[start:end]) != np.asarray(reference[start + offset : end + offset])
        padded = np.concatenate(([False], differ, [False]))
        changes = np.flatnonzero(padded[1:] != padded[:-1]).astype(np.int64) + start
        all_starts.append(changes[0::2])
        all_ends.append(changes[1::2])
    if not all_starts:
        empty = np.zeros(0, dtype=np.int64)
        return empty, empty
    starts, ends = np.concatenate(all_starts), np.concatenate(all_ends)
    # Join the runs cut by the block boundaries.
    joined = np.flatnonzero(starts[1:] == ends[:-1])
    if len(joined):
        keep = np.ones(len(starts), dtype=bool)
        keep[joined + 1] = False
        ends_keep = np.ones(len(ends), dtype=bool)
        ends_keep[joined] = False
        starts, ends = starts[keep], ends[ends_keep]
    return starts, ends


def compare_sessions(
    reference: CaptureSession,
    capture: CaptureSession,
    offset: int = 0,
    tolerance: int = 0,
    channel_map: Optional[dict[int, int]] = None,
) -> CompareResult:
    """Compare the channels of ``capture`` with those of ``reference``.

    Channels are matched by channel number, or ``channel_map`` (capture number →
    reference number). Differences of at most ``tolerance`` samples (edge jitter) are
    ignored.
    """
    offset, tolerance = int(offset), max(int(tolerance), 0)
    first, last = _overlap(reference.sample_count(), capture.sample_count(), offset)
    result = CompareResult(compared_samples=last - first, offset=offset, tolerance=tolerance)
    for number, samples, reference_samples in _pairs(reference, capture, channel_map):
        starts, ends = _difference_runs(samples, reference_samples, first, last, offset)
        if tolerance:
            kept = ends - starts > tolerance
            starts, ends = starts[kept], ends[kept]
        result.differences[number] = list(zip(starts.tolist(), ends.tolist()))
        result.differing_samples += int((ends - starts).sum())
    return result


def _edge_offsets(reference_edges: np.ndarray, capture_edges: np.ndarray, max_offset: int) -> np.ndarray:
    """``reference edge - capture edge`` of every pair at most ``max_offset`` apart
    (from the first capture edges, up to :data:`ALIGN_PAIRS` pairs)."""
    if not len(reference_edges) or not len(capture_edges):
        return np.zeros(0, dtype=np.int64)
    low = np.searchsorted(reference_edges, capture_edges - max_offset, side="left")
    high = np.searchsorted(reference_edges, capture_edges + max_offset, side="right")
    counts = high - low
    used = int(np.searchsorted(np.cumsum(counts), ALIGN_PAIRS, side="right"))
    used = max(used, 1)
    low, counts, capture_edges = low[:used], counts[:used], capture_edges[:used]
    total = int(counts.sum())
    if not total:
        return np.zeros(0, dtype=np.int64)
    owner = np.repeat(np.arange(used), counts)
    # Index of each pair inside the reference edges: low of its owner plus its rank.
    rank = np.arange(total) - np.repeat(np.cumsum(counts) - counts, counts)
    return reference_edges[low[owner] + rank] - capture_edges[owner]


def _channel_edges(samples: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    transitions = ChannelTransitions(samples, 1)
    positions = np.asarray(transitions.starts[1:], dtype=np.int64)
    levels = np.asarray(transitions.values[1:])
    return positions[levels != 0], positions[levels == 0]


def _check_differences(reference: CaptureSession, capture: CaptureSession, offset: int, pairs) -> float:
    """Share of differing samples at ``offset`` (over at most :data:`ALIGN_CHECK_SAMPLES`)."""
    first, last = _overlap(reference.sample_count(), capture.sample_count(), offset)
    last = min(last, first + ALIGN_CHECK_SAMPLES)
    if last <= first:
        return 1.0
    total = 0
    for samples, reference_samples in pairs:
        part = np.asarray(samples[first:last]) != np.asarray(reference_samples[first + offset : last + offset])
        total += int(np.count_nonzero(part))
    return total / ((last - first) * len(pairs))


def align_offset(
    reference: CaptureSession,
    capture: CaptureSession,
    max_offset: int,
    channel_map: Optional[dict[int, int]] = None,
) -> int:
    """Offset in ``[-max_offset, max_offset]`` (for :func:`compare_sessions`) at which
    the capture differs least from the reference; 0 without edges.

    The offsets at which most edges of the same kind coincide are candidates; the
    one with the fewest differing samples (ties: the smallest) wins.
    """
    max_offset = max(int(max_offset), 0)
    pairs = [(s, r) for _, s, r in _pairs(reference, capture, channel_map)]
    if not max_offset or not pairs:
        return 0
    votes = np.zeros(2 * max_offset + 1, dtype=np.int64)
    for samples, reference_samples in pairs:
        for capture_edges, reference_edges in zip(_channel_edges(samples), _channel_edges(reference_samples)):
            offsets = _edge_offsets(reference_edges, capture_edges, max_offset)
            votes += np.bincount(offsets + max_offset, minlength=len(votes))
    if not votes.any():
        return 0
    best = votes.max()
    # The best few offsets (also near misses, the edges may jitter) decide by their differences.
    candidates = np.flatnonzero(votes >= best * 0.9) - max_offset
    if len(candidates) > 16:
        candidates = candidates[np.argsort(-votes[candidates + max_offset], kind="stable")[:16]]
    candidates = np.union1d(candidates, [0])
    scores = [(_check_differences(reference, capture, int(c), pairs), abs(int(c)), int(c)) for c in candidates]
    return min(scores)[2]


def difference_regions(result: CompareResult, name: str = "Diff"):
    """Regions (``SampleRegion``) covering the differences of all channels, merged,
    named ``"Diff 1"``, ``"Diff 2"``…"""
    from PySide6.QtGui import QColor

    from .regions import SampleRegion

    ranges = [r for channel in result.differences.values() for r in channel]
    if not ranges:
        return []
    bounds = np.array(ranges, dtype=np.int64)
    bounds = bounds[np.argsort(bounds[:, 0], kind="stable")]
    starts, ends = bounds[:, 0], bounds[:, 1]
    reach = np.maximum.accumulate(ends)
    new = np.concatenate(([True], starts[1:] > reach[:-1]))
    group_starts = starts[new]
    group_ends = np.maximum.reduceat(ends, np.flatnonzero(new))
    return [
        SampleRegion(
            first_sample=int(start),
            last_sample=int(end),
            region_name=f"{name} {index}",
            region_color=QColor(*DIFFERENCE_COLOR),
        )
        for index, (start, end) in enumerate(zip(group_starts, group_ends), start=1)
    ]
