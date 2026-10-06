# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""When samples were taken: from when their blocks arrived, the command that started them, a
measured latency or a shared time scale (PTP, GPS). Standard library only: the same code runs in a
device script (a NI DAQ behind USB on a Raspberry Pi) and in openSciLab (its own instruments).

**Arrival.** A block can only arrive after its last sample was taken: ``taken <= arrived``. The
sample clock of the device is even, the arrivals are not (USB frames, buffers, the scheduler).
:class:`ArrivalClock` puts a line under the arrivals - their *lower envelope*: the line of the
fastest arrivals, with the slope of the sample clock (and its drift against this computer's clock
once the blocks span ``MIN_DRIFT_SPAN``). The jitter of the arrivals drops out; the shortest
latency remains: the line is an upper bound of the time a sample was taken.

**Start.** No sample is taken before the command that started the stream: a lower bound. Between
the two bounds the middle is the estimate, half their distance the uncertainty (the assumption of
NTP: starting takes about as long as delivering).

**Latency.** A loopback measures it: the device switches an output of its own at a time it stamps
(:class:`Loopback`), an input of the same device records the edge. The edge cannot be earlier than
the command, and its sample not later than the envelope: their distance is the latency of the
output plus the latency of the input. Half of the shortest one is the latency of the input
(:class:`Latency`), with half of it as the uncertainty. A latency measured once (or with a sync
signal) is kept for the settings it was measured with and used later without a wire.

**Time scales.** A device whose clock follows a shared time scale (UTC, TAI: PTP, GPS, NTP) stamps
its samples in it; a computer whose clock follows it too converts them without measuring anything
(:func:`timescale_now`, :func:`local_offset`).
"""

from __future__ import annotations

import math
import random
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Iterator, List, Optional, Sequence, Tuple

#: the drift of the sample clock is fitted only over blocks that span at least this (seconds)
MIN_DRIFT_SPAN = 10.0
#: the fastest arrivals kept for the envelope: one per slot of samples (slots grow as needed)
WINDOW = 512
#: arrivals older than this (seconds of samples) are forgotten: the drift may change slowly
MAX_SPAN = 300.0
#: parts of the window whose fastest arrival makes the envelope (with drift)
BINS = 8
#: recent blocks kept for the jitter
RECENT = 64

#: time scales a device clock can follow
TIMESCALES = ("utc", "tai")
#: TAI - UTC since 2017 (37 leap seconds); used where the system has no TAI clock
TAI_MINUS_UTC = 37.0


def fit_line(points: Sequence[Tuple[float, float]], reference: float = 0.0) -> Tuple[float, float, float]:
    """Least squares line ``y = a + b (x - reference)``: ``(a, b, residual std)``."""
    count = len(points)
    if count == 0:
        raise ValueError("no points")
    if count == 1:
        return points[0][1], 0.0, 0.0
    xs = [x - reference for x, _y in points]
    ys = [y for _x, y in points]
    mean_x, mean_y = sum(xs) / count, sum(ys) / count
    spread = sum((x - mean_x) ** 2 for x in xs)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / spread if spread > 1e-18 else 0.0
    intercept = mean_y - slope * mean_x
    residuals = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
    std = math.sqrt(sum(r * r for r in residuals) / max(count - 2, 1)) if count > 2 else 0.0
    return intercept, slope, std


@dataclass(frozen=True)
class Latency:
    """How long samples take to arrive (seconds), ``± uncertainty``; ``source``: how it was found
    (``loopback``, ``signal``, ``given``)."""

    value: float
    uncertainty: float = 0.0
    source: str = "given"

    def to_dict(self) -> dict:
        return {"value": self.value, "uncertainty": self.uncertainty, "source": self.source}

    @staticmethod
    def from_dict(data: dict) -> Latency:
        return Latency(float(data["value"]), float(data.get("uncertainty", 0.0)), str(data.get("source", "given")))


@dataclass(frozen=True)
class Estimate:
    """The time of a sample: ``time ± uncertainty`` (``inf``: only an upper bound is known)."""

    time: float
    uncertainty: float
    #: ``arrival`` (only the envelope), ``bounds`` (start and arrival), ``latency`` (a measured
    #: latency), ``timescale`` (stamped by the device in a shared time scale)
    method: str


class ArrivalClock:
    """When the samples of a stream at ``rate`` were taken, from when its blocks arrived.

    :meth:`add` every block (the index of its last sample, the time it arrived); :meth:`start`
    the time the stream was started, if known. Only the fastest arrival of every slot of samples
    is kept, so the envelope reaches back minutes with little memory. Not thread safe: one
    stream, one thread."""

    def __init__(self, rate: float, window: int = WINDOW) -> None:
        if not rate > 0:
            raise ValueError("the rate must be positive")
        self.rate = float(rate)
        self.window = int(window)
        #: slot -> (index, arrived) of the fastest arrival in it
        self._slots: dict = {}
        self._slot_size = 1.0
        self._recent: deque = deque(maxlen=RECENT)
        #: time the stream was started (no sample was taken before it)
        self.started: Optional[float] = None
        self._a = 0.0
        self._b = 1.0 / self.rate
        self._dirty = False
        self.blocks = 0
        self._last = 0.0

    def start(self, at: float) -> None:
        self.started = float(at)

    def add(self, last_index: int, arrived: float) -> None:
        """A block whose last sample has ``last_index`` (counted from the start) arrived at ``arrived``."""
        index, arrived = float(last_index), float(arrived)
        nominal = 1.0 / self.rate
        residual = arrived - index * nominal
        self._recent.append((index, arrived))
        self.blocks += 1
        self._last = max(self._last, index)
        if self.blocks == 1:
            # slots of about a block: the first block tells how large they are
            self._slot_size = max(index + 1.0, 1.0)
        key = int(index // self._slot_size)
        current = self._slots.get(key)
        if current is None or residual < current[1] - current[0] * nominal:
            self._slots[key] = (index, arrived)
        oldest = self._last - MAX_SPAN * self.rate
        if any(point[0] < oldest for point in self._slots.values()):
            self._slots = {slot: point for slot, point in self._slots.items() if point[0] >= oldest}
        while len(self._slots) > self.window:
            self._slot_size *= 2
            merged: dict = {}
            for point in self._slots.values():
                slot = int(point[0] // self._slot_size)
                kept = merged.get(slot)
                if kept is None or point[1] - point[0] * nominal < kept[1] - kept[0] * nominal:
                    merged[slot] = point
            self._slots = merged
        self._dirty = True

    @property
    def ready(self) -> bool:
        return bool(self._slots)

    def _update(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        points = sorted(self._slots.values())
        nominal = 1.0 / self.rate
        span = (points[-1][0] - points[0][0]) * nominal
        slope = 0.0
        if span >= MIN_DRIFT_SPAN and len(points) >= 2 * BINS:
            # the fastest arrival of every part of the window: a line through them gives the drift
            first, last = points[0][0], points[-1][0]
            width = (last - first) / BINS or 1.0
            lowest: dict = {}
            for index, arrived in points:
                part = min(int((index - first) / width), BINS - 1)
                residual = arrived - index * nominal
                if part not in lowest or residual < lowest[part][1]:
                    lowest[part] = (index, residual)
            _a, slope, _std = fit_line(list(lowest.values()), first)
        b = nominal + slope
        # the line touches the fastest arrival from below: no block is earlier than the envelope
        self._a = min(arrived - index * b for index, arrived in points)
        self._b = b

    def envelope(self, index: float) -> float:
        """The latest time the sample ``index`` can have been taken (it plus the shortest latency)."""
        if not self._slots:
            raise ValueError("no block arrived yet")
        self._update()
        return self._a + self._b * float(index)

    def earliest(self, index: float) -> Optional[float]:
        """The earliest time the sample ``index`` can have been taken (``None``: no start known)."""
        if self.started is None:
            return None
        self._update()
        return self.started + self._b * float(index)

    @property
    def period(self) -> float:
        """Seconds per sample on this computer's clock (the drift of the sample clock included)."""
        self._update()
        return self._b

    @property
    def drift(self) -> float:
        """How much faster the sample clock runs than this computer's clock (``1e-6``: 1 ppm)."""
        return (1.0 / self.rate) / self.period - 1.0

    def jitter(self) -> float:
        """The median of how late the blocks arrived above the envelope."""
        if not self._recent:
            return 0.0
        self._update()
        late = sorted(arrived - (self._a + self._b * index) for index, arrived in self._recent)
        return late[len(late) // 2]

    def estimate(self, index: float, latency: Optional[Latency] = None) -> Estimate:
        """The time the sample ``index`` was taken: the envelope less a measured ``latency``, else the
        middle between start and envelope, else the envelope (an upper bound)."""
        high = self.envelope(index)
        low = self.earliest(index)
        if latency is not None:
            value = high - latency.value
            if low is not None:
                value = min(max(value, low), high)
            return Estimate(value, latency.uncertainty, "latency")
        if low is not None and low <= high:
            return Estimate((low + high) / 2, (high - low) / 2, "bounds")
        return Estimate(high, math.inf, "arrival")


class Loopback:
    """Measures the latency of an input with a loopback: :meth:`command` the time an output of the
    device was switched (stamped right before), :meth:`edge` the index of the sample of the input
    where the edge appears; :meth:`latency` with the :class:`ArrivalClock` of the input."""

    def __init__(self) -> None:
        self._commands: List[float] = []
        self._pairs: List[Tuple[float, float]] = []

    def command(self, at: float) -> None:
        self._commands.append(float(at))

    def edge(self, index: float, command: Optional[float] = None) -> None:
        """The edge of the last command (or of ``command``) appeared at the sample ``index``."""
        if command is None:
            if not self._commands:
                raise ValueError("an edge without a command")
            command = self._commands[-1]
        self._pairs.append((float(command), float(index)))

    @property
    def count(self) -> int:
        return len(self._pairs)

    def latency(self, clock: ArrivalClock) -> Latency:
        """Half the shortest loop (output + input latency), ± half of it."""
        if not self._pairs:
            raise ValueError("no edge was seen: is the output wired to the input?")
        loop = min(clock.envelope(index) - command for command, index in self._pairs)
        loop = max(loop, 0.0)
        return Latency(loop / 2, loop / 2, "loopback")


def sync_intervals(seed: int = 1, minimum: float = 0.02, maximum: float = 0.06) -> Iterator[float]:
    """Irregular intervals for a sync signal (the same ``seed``, the same intervals): edges with
    distances that differ can be matched without ambiguity."""
    generator = random.Random(seed)
    while True:
        yield generator.uniform(minimum, maximum)


# ----------------------------------------------------------------- time scales
def timescale_now(name: str) -> float:
    """Seconds of the time scale ``name`` (``utc``: since 1970; ``tai``: UTC + 37 s, from the TAI clock
    of Linux when it is set)."""
    if name == "utc":
        return time.time()
    if name == "tai":
        clock_tai = getattr(time, "CLOCK_TAI", None)
        if clock_tai is not None:
            try:
                value = time.clock_gettime(clock_tai)
                if abs(value - time.time() - TAI_MINUS_UTC) < 2.0:  # set (else it equals UTC)
                    return value
            except OSError:
                pass
        return time.time() + TAI_MINUS_UTC
    raise ValueError(f"time scale is one of {', '.join(TIMESCALES)}, not {name!r}")


def timescale_clock(name: str) -> Callable[[], float]:
    """A clock for ``Device(clock=..., timescale=name)``."""
    timescale_now(name)  # (checks the name)
    return lambda: timescale_now(name)


def local_offset(name: str, local: Callable[[], float] = time.monotonic, reads: int = 5) -> float:
    """``timescale - local`` now: of a few readings the one with the shortest bracket."""
    best: Optional[Tuple[float, float]] = None
    for _ in range(max(reads, 1)):
        before = local()
        value = timescale_now(name)
        after = local()
        if best is None or after - before < best[0]:
            best = (after - before, value - (before + after) / 2)
    return best[1]
