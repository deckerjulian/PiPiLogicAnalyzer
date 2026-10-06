# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The clock of a remote device as openSciLab sees it: ``remote = local + offset + drift * (local - t_ref)``.

**Over the network** (as NTP): openSciLab sends a ping at ``t0`` (its clock), the device stamps its
arrival ``t1`` and its answer ``t2`` (its clock), the answer arrives at ``t3``. Then::

    offset = ((t1 - t0) + (t2 - t3)) / 2      delay = (t3 - t0) - (t2 - t1)

A measurement is only as good as its delay is short (queues on the way make it unsymmetric), so
the model uses the measurements with the shortest delays of a sliding window and fits a line
through their offsets: offset and drift. Its uncertainty is half the delay of those measurements
plus the scatter around the line. A jump (the device slept, its clock was set) shows as offsets
far from the line again and again; the model then starts anew.

**Over a synchronisation signal** the same edges are seen by the device (its clock) and by a
channel openSciLab records (local time): a line through those pairs gives offset and drift with
the precision of the recording (only the offset when the device's sample clock is shared with the
recording); it replaces the other estimates until it is too old.

**Over a time scale**: a device whose clock follows UTC or TAI (PTP, GPS) on a computer whose clock
follows it too needs no measurement: its times are converted with the computer's own offset of the
time scale. The network measurement goes on as a check (:attr:`ClockState.check`).

Thread safe; Qt free.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass
from typing import Optional, Sequence

from openscilab_device.timing import fit_line

#: measurements kept (one a second after the first burst: about two minutes)
WINDOW = 120
#: share of the measurements (those with the shortest delay) the fit uses
BEST_SHARE = 0.3
#: offsets this far (seconds) from the line count as a jump
JUMP = 0.005
#: a jump is a jump after this many measurements in a row far from the line
JUMP_COUNT = 3
#: the drift is fitted only over measurements at least this far apart (seconds)
MIN_DRIFT_SPAN = 10.0
#: a fit over the sync signal is used for this long (seconds) after its last edge
SIGNAL_VALID = 30.0

METHOD_NONE = "none"
METHOD_NETWORK = "network"
METHOD_SIGNAL = "signal"
METHOD_TIMESCALE = "timescale"


@dataclass(frozen=True)
class Sample:
    local: float
    offset: float
    delay: float


@dataclass(frozen=True)
class ClockState:
    """What the device card and the flows show about a remote clock."""

    method: str
    offset: float
    drift: float
    #: how far a converted time may be off (seconds, ±)
    uncertainty: float
    #: shortest and typical round trip (seconds)
    delay: float
    jitter: float
    samples: int
    jumps: int
    #: the network estimate less the one used (signal or time scale), NaN without both
    check: float = math.nan
    #: the time scale of the device (``utc``, ``tai``) when it is used
    timescale: str = ""


class ClockModel:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._samples: deque[Sample] = deque(maxlen=WINDOW)
        self._far: list[Sample] = []
        self.jumps = 0
        # the network line: offset at reference, drift
        self._reference = 0.0
        self._offset = 0.0
        self._drift = 0.0
        self._uncertainty = math.inf
        # the line of the sync signal (when there is one)
        self._signal: Optional[tuple[float, float, float, float, float]] = None  # reference, offset, drift, std, last
        # the time scale: name, uncertainty, function giving its offset (time scale - local) now
        self._timescale: Optional[tuple] = None

    # ---------------------------------------------------------- measuring
    def add(self, t0: float, t1: float, t2: float, t3: float) -> Sample:
        """A ping: ``t0``/``t3`` local, ``t1``/``t2`` of the device."""
        delay = max((t3 - t0) - (t2 - t1), 0.0)
        sample = Sample(local=(t0 + t3) / 2, offset=((t1 - t0) + (t2 - t3)) / 2, delay=delay)
        with self._lock:
            if len(self._samples) >= 8:
                predicted = self._offset + self._drift * (sample.local - self._reference)
                tolerance = max(JUMP, 4 * self._uncertainty, 2 * sample.delay)
                if abs(sample.offset - predicted) > tolerance:
                    # far from the line: kept aside (it must not pull the line) until it happens again
                    self._far.append(sample)
                    if len(self._far) < JUMP_COUNT:
                        return sample
                    self.jumps += 1  # the clock jumped: what was measured before is void
                    self._samples.clear()
                    self._samples.extend(self._far)
                    self._signal = None
                    self._far = []
                    self._update()
                    return sample
                self._far = []
            self._samples.append(sample)
            self._update()
        return sample

    def _update(self) -> None:
        samples = list(self._samples)
        if not samples:
            return
        count = max(6 if len(samples) >= 12 else 3, int(len(samples) * BEST_SHARE)) if len(samples) >= 3 \
            else len(samples)
        best = sorted(samples, key=lambda item: item.delay)[:count]
        self._reference = samples[-1].local
        offset, drift, std = fit_line([(item.local, item.offset) for item in best], self._reference)
        span = max(item.local for item in best) - min(item.local for item in best)
        if len(best) < 6 or span < MIN_DRIFT_SPAN:
            # too few, or too close together for a slope (a jitter of 1 ms over 1 s is 1000 ppm): the mean
            drift = 0.0
            offset = sum(item.offset for item in best) / len(best)
        self._offset, self._drift = offset, drift
        self._uncertainty = min(item.delay for item in best) / 2 + std

    def set_signal(self, pairs: Sequence[tuple[float, float]], fit_drift: bool = True) -> float:
        """Edges of the sync signal: ``(local time, device time)``; returns the scatter (s). Without
        ``fit_drift`` (the sample clock is shared with the recording) only the offset is fitted."""
        if len(pairs) < 2 if fit_drift else not pairs:
            raise ValueError("at least two edges")
        reference = pairs[-1][0]
        differences = [(local, remote - local) for local, remote in pairs]
        if fit_drift:
            offset, drift, std = fit_line(differences, reference)
        else:
            values = [difference for _local, difference in differences]
            offset, drift = sum(values) / len(values), 0.0
            std = math.sqrt(sum((value - offset) ** 2 for value in values) / max(len(values) - 1, 1)) \
                if len(values) > 1 else 0.0
        with self._lock:
            self._signal = (reference, offset, drift, std, reference)
        return std

    def set_timescale(self, name: str, uncertainty: float, offset) -> None:
        """The device's clock follows the time scale ``name``; ``offset()`` is ``time scale - local``
        of this computer now (``None``: forget it)."""
        with self._lock:
            self._timescale = (name, float(uncertainty), offset) if name else None

    # ---------------------------------------------------------- converting
    def _line(self, local: float) -> tuple[float, float, str]:
        if self._signal is not None and local - self._signal[4] <= SIGNAL_VALID:
            reference, offset, drift, _std, _last = self._signal
            return offset + drift * (local - reference), drift, METHOD_SIGNAL
        if self._timescale is not None:
            return self._timescale[2](), 0.0, METHOD_TIMESCALE
        if not self._samples:
            return 0.0, 0.0, METHOD_NONE
        return self._offset + self._drift * (local - self._reference), self._drift, METHOD_NETWORK

    def to_remote(self, local: float) -> float:
        with self._lock:
            offset, _drift, _method = self._line(local)
        return local + offset

    def to_local(self, remote: float) -> float:
        """The local time of a device time (the line solved for local time)."""
        with self._lock:
            guess = remote
            for _ in range(3):  # converges at once: the drift is tiny
                offset, _drift, _method = self._line(guess)
                guess = remote - offset
        return guess

    @property
    def ready(self) -> bool:
        with self._lock:
            return bool(self._samples) or self._signal is not None or self._timescale is not None

    def state(self, local: Optional[float] = None) -> ClockState:
        with self._lock:
            samples = list(self._samples)
            at = local if local is not None else (samples[-1].local if samples else 0.0)
            offset, drift, method = self._line(at)
            if method == METHOD_SIGNAL:
                uncertainty = self._signal[3] * 2 if self._signal else 0.0
            elif method == METHOD_TIMESCALE:
                uncertainty = self._timescale[1]
            else:
                uncertainty = self._uncertainty if samples else math.inf
            check = math.nan
            if method != METHOD_NETWORK and samples:
                check = self._offset + self._drift * (at - self._reference) - offset
            delays = sorted(item.delay for item in samples)
            delay = delays[0] if delays else math.inf
            typical = delays[len(delays) // 2] if delays else math.inf
            return ClockState(method, offset, drift, uncertainty, delay, max(typical - delay, 0.0), len(samples),
                              self.jumps, check, self._timescale[0] if method == METHOD_TIMESCALE else "")

    def typical_delay(self) -> float:
        """A one way delay that most values arrive within (for ordering values of several devices)."""
        with self._lock:
            delays = sorted(item.delay for item in self._samples)
        if not delays:
            return 0.05
        return delays[min(int(len(delays) * 0.95), len(delays) - 1)] / 2
