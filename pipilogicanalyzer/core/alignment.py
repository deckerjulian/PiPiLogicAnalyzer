# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Aligning the boards of a multi device capture.

The master triggers the other boards through a wire; the driver compensates the fixed delay of
the trigger programs, but every slave still starts up to a sample early or late, and the crystals
of the boards differ by some ppm (up to a few samples over a full buffer). Both are corrected here
after the capture, in one of two ways:

``reference``
    A signal captured by a slave is also connected to a free input of the master, named like the
    slave channel plus `` ref`` (e.g. ``A0 (Y) ref``). The offset between both copies is measured in
    several windows; a straight line through them gives offset and drift. Exact, needs one wire.

``clock``
    Without a usable reference line the offset is estimated from a clock on the master (a channel
    named ``Φ2``, ``PHI2``, ``CLK`` or ``clock``): synchronous address lines only change after the
    falling clock edge, so a slave whose address transitions fall into the samples just before the
    edges is moved by the smallest amount that clears them. An estimate, needs no hardware change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

from ..driver.models import AnalyzerChannel, CaptureSession

#: Samples searched in either direction.
MAX_SHIFT = 8
#: Windows the capture is split into for the drift estimate.
WINDOWS = 4
REFERENCE_SUFFIX = " ref"
#: Share of the slave edges that must coincide with the reference copy.
MIN_REFERENCE_MATCH = 0.8
#: Edges a window needs to be evaluated.
MIN_EDGES = 20
#: Samples before a falling clock edge in which a synchronous bus must not change.
SETUP_SAMPLES = 2
#: Samples after the clock edge in which the bus normally changes.
CHANGE_SAMPLES = 4

CLOCK_NAME = re.compile(r"^\s*(φ2|Φ2|phi\s*2|clk|clock)", re.IGNORECASE)
ADDRESS_NAME = re.compile(r"^\s*A\d+\b")


@dataclass
class DeviceAlignment:
    device: int
    method: str  # "reference" or "clock"
    source: str  # the reference or clock channel
    #: Samples the board is moved later at the start of the capture (negative: earlier).
    offset: float
    #: Additional samples per sample (clock drift).
    drift: float
    sample_count: int

    @property
    def changed(self) -> bool:
        return round(self.offset) != 0 or round(self.offset + self.drift * self.sample_count) != 0

    def describe(self) -> str:
        how = f"reference {self.source}" if self.method == "reference" else f"clock {self.source}"
        if not self.changed:
            return f"board {self.device + 1} already aligned ({how})"
        text = f"board {self.device + 1} moved by {self.offset:+.0f} samples"
        total = self.drift * self.sample_count
        if round(total) != 0:
            text += f", drift {total:+.1f} samples over the capture"
        return f"{text} ({how})"


def transitions(samples: np.ndarray) -> np.ndarray:
    """Samples at which the level differs from the previous sample."""
    values = np.asarray(samples, dtype=np.int8)
    return np.flatnonzero(values[1:] != values[:-1]) + 1


def falling_edges(samples: np.ndarray) -> np.ndarray:
    values = np.asarray(samples, dtype=np.int8)
    return np.flatnonzero((values[:-1] == 1) & (values[1:] == 0)) + 1


def device_of(channel: AnalyzerChannel, channels_per_device: int) -> int:
    return channel.channel_number // max(channels_per_device, 1)


def _windows(count: int) -> list[tuple[int, int]]:
    size = max(count // WINDOWS, 1)
    return [(start, min(start + size, count)) for start in range(0, count, size)][:WINDOWS]


def _fit(points: list[tuple[float, int]], count: int) -> tuple[float, float]:
    """Offset at sample 0 and drift per sample through (window centre, shift) points."""
    if len(points) == 1:
        return float(points[0][1]), 0.0
    centres = np.array([point[0] for point in points])
    shifts = np.array([point[1] for point in points], dtype=float)
    if np.ptp(shifts) <= 0:
        return float(shifts[0]), 0.0
    slope, intercept = np.polyfit(centres, shifts, 1)
    return float(intercept), float(slope)


# --------------------------------------------------------------------- reference
def _reference_shift(master: np.ndarray, slave: np.ndarray) -> tuple[Optional[float], float]:
    """Shift moving the slave edges onto the master edges and the share that coincides.

    The median distance to the nearest master edge is used instead of the best integer shift, so
    a board drifting by a sample within the window still matches and the drift fit gets
    fractional values.
    """
    if len(slave) < MIN_EDGES or len(master) < MIN_EDGES:
        return None, 0.0
    index = np.clip(np.searchsorted(master, slave), 1, len(master) - 1)
    before = master[index - 1] - slave
    after = master[index] - slave
    nearest = np.where(np.abs(before) <= np.abs(after), before, after)
    close = nearest[np.abs(nearest) <= MAX_SHIFT]
    if close.size < MIN_EDGES:
        return None, 0.0
    shift = float(np.median(close))
    return shift, np.count_nonzero(np.abs(nearest - shift) <= 1) / len(slave)


def align_by_reference(
    reference: AnalyzerChannel, target: AnalyzerChannel, device: int, count: int
) -> Optional[DeviceAlignment]:
    master_edges = transitions(reference.samples[:count])
    slave_edges = transitions(target.samples[:count])
    points = []
    for first, last in _windows(count):
        shift, match = _reference_shift(
            master_edges[(master_edges >= first) & (master_edges < last)],
            slave_edges[(slave_edges >= first) & (slave_edges < last)],
        )
        if shift is not None and match >= MIN_REFERENCE_MATCH:
            points.append(((first + last) / 2.0, shift))
    if not points:
        return None
    offset, drift = _fit(points, count)
    return DeviceAlignment(device, "reference", reference.channel_name, offset, drift, count)


# ------------------------------------------------------------------------- clock
def _setup_violations(edges: np.ndarray, changes: np.ndarray) -> int:
    """Bus changes from ``SETUP_SAMPLES`` before a falling clock edge up to the edge sample.

    A change in the edge sample itself counts as well: the decoder reads the sample before the
    edge and checks its neighbours, so the bus has to change after the edge sample.
    """
    if not len(edges) or not len(changes):
        return 0
    index = np.searchsorted(edges, changes, side="left")
    valid = index < len(edges)
    distance = edges[index[valid]] - changes[valid]
    return int(np.count_nonzero((distance >= 0) & (distance <= SETUP_SAMPLES)))


def _changes_after_edges(edges: np.ndarray, changes: np.ndarray) -> int:
    index = np.searchsorted(edges, changes, side="left") - 1
    valid = index >= 0
    distance = changes[valid] - edges[index[valid]]
    return int(np.count_nonzero((distance >= 1) & (distance <= CHANGE_SAMPLES)))


def _clock_shift(edges: np.ndarray, changes: np.ndarray) -> Optional[int]:
    if len(edges) < MIN_EDGES or len(changes) < MIN_EDGES:
        return None
    tolerance = max(2, int(0.02 * len(changes)))
    if _setup_violations(edges, changes) <= tolerance:
        return 0
    scores = []
    for shift in range(-MAX_SHIFT, MAX_SHIFT + 1):
        moved = changes + shift
        scores.append((shift, _setup_violations(edges, moved), _changes_after_edges(edges, moved)))
    clean = [score for score in scores if score[1] <= tolerance]
    if clean:
        # The bus changes right after the clock edge: prefer shifts that put the changes there.
        return max(clean, key=lambda score: (score[2], -abs(score[0])))[0]
    return min(scores, key=lambda score: (score[1], abs(score[0])))[0]


def align_by_clock(
    clock: AnalyzerChannel, channels: Sequence[AnalyzerChannel], device: int, count: int
) -> Optional[DeviceAlignment]:
    bus = [channel for channel in channels if ADDRESS_NAME.match(channel.channel_name or "")] or list(channels)
    edges = falling_edges(clock.samples[:count])
    changes = np.sort(np.concatenate([transitions(channel.samples[:count]) for channel in bus]))
    points = []
    for first, last in _windows(count):
        shift = _clock_shift(
            edges[(edges >= first) & (edges < last)],
            changes[(changes >= first) & (changes < last)],
        )
        if shift is not None:
            points.append(((first + last) / 2.0, shift))
    if not points:
        return None
    shifts = [shift for _centre, shift in points]
    if max(shifts) - min(shifts) > 2:
        # The windows disagree, a drift fit would amplify the noise: use the most common shift.
        values, counts = np.unique(shifts, return_counts=True)
        return DeviceAlignment(device, "clock", clock.channel_name, float(values[np.argmax(counts)]), 0.0, count)
    offset, drift = _fit(points, count)
    return DeviceAlignment(device, "clock", clock.channel_name, offset, drift, count)


# ------------------------------------------------------------------------ public
def apply_alignment(session: CaptureSession, channels_per_device: int, alignment: DeviceAlignment) -> None:
    """Resample the channels of ``alignment.device`` (nearest sample)."""
    for channel in session.capture_channels:
        if device_of(channel, channels_per_device) != alignment.device or channel.samples is None:
            continue
        count = channel.samples.size
        positions = np.arange(count)
        shift = np.rint(alignment.offset + alignment.drift * positions).astype(np.int64)
        channel.samples = channel.samples[np.clip(positions - shift, 0, count - 1)]


def alignment_options(session: CaptureSession, channels_per_device: int) -> dict[int, list[DeviceAlignment]]:
    """Every method that can align each slave board, the preferred one first (nothing is changed)."""
    channels = [channel for channel in session.capture_channels if channel.samples is not None]
    if not channels:
        return {}
    count = min(channel.samples.size for channel in channels)
    master = [channel for channel in channels if device_of(channel, channels_per_device) == 0]
    references = {
        (channel.channel_name or "")[: -len(REFERENCE_SUFFIX)].strip().lower(): channel
        for channel in master
        if (channel.channel_name or "").lower().endswith(REFERENCE_SUFFIX)
    }
    clock = next((channel for channel in master if CLOCK_NAME.match(channel.channel_name or "")), None)

    options: dict[int, list[DeviceAlignment]] = {}
    for device in sorted({device_of(channel, channels_per_device) for channel in channels} - {0}):
        board = [channel for channel in channels if device_of(channel, channels_per_device) == device]
        candidates = []
        for channel in board:
            reference = references.get((channel.channel_name or "").strip().lower())
            if reference is not None:
                result = align_by_reference(reference, channel, device, count)
                if result is not None:
                    candidates.append(result)
        if clock is not None:
            result = align_by_clock(clock, board, device, count)
            if result is not None:
                candidates.append(result)
        if candidates:
            options[device] = candidates
    return options


def align_devices(
    session: CaptureSession, channels_per_device: int, apply: bool = True
) -> list[DeviceAlignment]:
    """Measure (and correct) the offset of every slave board with the preferred method."""
    results = []
    for _device, candidates in alignment_options(session, channels_per_device).items():
        chosen = candidates[0]
        if apply and chosen.changed:
            apply_alignment(session, channels_per_device, chosen)
        results.append(chosen)
    return results


def channels_per_device_of(session: CaptureSession, default: int = 24) -> Optional[int]:
    """Channels per board of a capture from a multi device set (``None`` for a single board)."""
    if session.threshold_voltage is not None:
        return None  # a device with an adjustable threshold (DSLogic): one board of up to 32 channels
    if any(channel.channel_number >= default for channel in session.capture_channels):
        return default
    return None
