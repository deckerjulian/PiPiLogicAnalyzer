# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""When the samples of openSciLab's own instruments were taken, on this computer's clock.

A capture or stream of an instrument is an :class:`Acquisition`: its samples count from the first
one at the device's rate; the acquisition knows where sample ``k`` lies on the local clock
(``time.monotonic``) and how well. Best first:

* **simulated** - a simulator knows its time (unless it emulates USB, ``usb`` in its profile);
* **signal** - aligned with a sync signal that a reference instrument recorded too
  (``timing.align``): offset and drift against the reference, with the precision of the recordings;
* **timescale** - the device stamps its samples in a shared time scale (PTP, GPS) and this
  computer's clock follows it too (preference ``timing.host_clock``);
* **latency** - the arrival envelope of the blocks less a latency measured before (loopback,
  ``timing.calibrate``), kept per instrument and settings in ``timing.json``;
* **bounds** - between the command that started it and the envelope of the arrivals;
* **arrival** - only the envelope (an upper bound).

The algorithms are those of :mod:`openscilab_device.timing` (a device script uses them for its
own samples). Qt free.
"""

from __future__ import annotations

import bisect
import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np

from openscilab_device.timing import (  # noqa: F401
    ArrivalClock,
    Estimate,
    Latency,
    Loopback,
    fit_line,
    local_offset,
)

from . import preferences, settings

TIMING_FILE = "timing.json"

METHOD_SIMULATED = "simulated"
METHOD_SIGNAL = "signal"
METHOD_TIMESCALE = "timescale"
METHOD_LATENCY = "latency"
METHOD_BOUNDS = "bounds"
METHOD_ARRIVAL = "arrival"

METHOD_TEXT = {
    METHOD_SIMULATED: "simulated (exact)",
    METHOD_SIGNAL: "sync signal",
    METHOD_TIMESCALE: "time scale of the device (PTP/GPS)",
    METHOD_LATENCY: "arrival less the measured latency",
    METHOD_BOUNDS: "between start command and arrival",
    METHOD_ARRIVAL: "arrival only (an upper bound)",
}

#: how close a clock that follows NTP or PTP usually is (seconds), when no accuracy is set
HOST_ACCURACY = {"ntp": 2e-3, "ptp": 50e-6}
#: blocks of an acquisition remembered (to find the sample index of an emitted time)
HISTORY = 4096


# ---------------------------------------------------------------- host clock
def host_timescale_accuracy() -> Optional[float]:
    """How close this computer's clock is to UTC/TAI (``None``: it follows no time scale)."""
    kind = preferences.get("timing.host_clock")
    if kind not in HOST_ACCURACY:
        return None
    accuracy = float(preferences.get("timing.host_accuracy") or 0.0)
    return accuracy if accuracy > 0 else HOST_ACCURACY[kind]


_offsets: dict[str, tuple[float, float]] = {}
_offsets_lock = threading.Lock()


def timescale_to_local(value: float, timescale: str) -> float:
    """A time of the time scale as local time (``time.monotonic``); the offset is read again every
    second (NTP and PTP slew the clock)."""
    now = time.monotonic()
    with _offsets_lock:
        cached = _offsets.get(timescale)
    if cached is None or now - cached[0] > 1.0:
        cached = (now, local_offset(timescale))
        with _offsets_lock:
            _offsets[timescale] = cached
    return value - cached[1]


# ------------------------------------------------------------ calibrations
def calibration_key(instrument: Any, mode: str, rate: float, samples: int = 0) -> str:
    """What a latency belongs to: the instrument (kind and address), the acquisition mode, the rate
    and - for a capture, whose transfer grows with it - the length."""
    kind = getattr(instrument, "kind", "") or ""
    uri = getattr(instrument, "uri", "") or getattr(instrument, "name", "")
    length = f"|{int(samples)}" if mode != "stream" and samples else ""
    return f"{kind}|{uri}|{mode}|{float(rate):g}{length}"


def stored_latency(key: str) -> Optional[Latency]:
    data = settings.get_settings(TIMING_FILE) or {}
    entry = (data.get("latency") or {}).get(key) if isinstance(data, dict) else None
    try:
        return Latency.from_dict(entry) if isinstance(entry, dict) else None
    except (KeyError, TypeError, ValueError):
        return None


def store_latency(key: str, latency: Optional[Latency]) -> bool:
    """Keep ``latency`` for ``key`` (``None``: forget it)."""
    data = settings.get_settings(TIMING_FILE)
    data = data if isinstance(data, dict) else {}
    entries = dict(data.get("latency") or {})
    if latency is None:
        entries.pop(key, None)
    else:
        entries[key] = {**latency.to_dict(), "measured": time.time()}
    data["latency"] = entries
    return settings.persist_settings(TIMING_FILE, data)


def latencies_of(instrument: Any) -> dict[str, Latency]:
    """The latencies kept for ``instrument`` by what they belong to (``"stream at 1 MHz"``)."""
    prefix = calibration_key(instrument, "", 0).rsplit("|", 2)[0] + "|"
    data = settings.get_settings(TIMING_FILE) or {}
    found: dict[str, Latency] = {}
    for key, entry in sorted(((data.get("latency") or {}) if isinstance(data, dict) else {}).items()):
        if not key.startswith(prefix) or not isinstance(entry, dict):
            continue
        mode, rate, *length = key[len(prefix):].split("|")
        try:
            from . import units

            title = f"{mode} at {units.format_quantity(float(rate), 'Hz')}" + (
                f", {int(length[0]):,} samples" if length else "")
            found[title] = Latency.from_dict(entry)
        except (KeyError, TypeError, ValueError):
            continue
    return found


def forget_latencies(instrument: Any) -> int:
    """Forget the latencies kept for ``instrument``; returns how many there were."""
    prefix = calibration_key(instrument, "", 0).rsplit("|", 2)[0] + "|"
    data = settings.get_settings(TIMING_FILE)
    data = data if isinstance(data, dict) else {}
    entries = dict(data.get("latency") or {})
    mine = [key for key in entries if key.startswith(prefix)]
    for key in mine:
        del entries[key]
    if mine:
        data["latency"] = entries
        settings.persist_settings(TIMING_FILE, data)
    return len(mine)


def host_clock_text() -> str:
    """What this computer's clock follows, for people (preference ``timing.host_clock``)."""
    kind = preferences.get("timing.host_clock")
    accuracy = host_timescale_accuracy()
    if accuracy is None:
        return "free running (no NTP or PTP set in Settings → Time)"
    return f"follows {str(kind).upper()}, ± {accuracy * 1e3:g} ms"


# ------------------------------------------------------------ the samples
@dataclass(frozen=True)
class Correction:
    """``reference time = raw time + offset + drift * (raw time - at)`` (from a sync signal)."""

    offset: float
    drift: float
    at: float
    uncertainty: float
    reference: str = ""
    edges: int = 0

    def apply(self, raw: float) -> float:
        return raw + self.offset + self.drift * (raw - self.at)


@dataclass(frozen=True)
class TimingState:
    """What the device card shows about the time of an instrument's samples."""

    method: str
    uncertainty: float
    latency: Optional[Latency] = None
    drift: float = 0.0
    jitter: float = 0.0
    reference: str = ""

    @property
    def text(self) -> str:
        return METHOD_TEXT.get(self.method, self.method)


class Acquisition:
    """One capture or stream: where its sample ``k`` lies on the local clock.

    ``commanded``: local time right before the device was told to start (no sample is earlier).
    Blocks of a stream are :meth:`arrived`; a capture arrives once, complete. A simulator that
    knows its time gives ``exact_start``; a device that stamps in a time scale ``device_start``."""

    def __init__(self, rate: float, commanded: float, latency: Optional[Latency] = None,
                 exact_start: Optional[float] = None, name: str = "") -> None:
        self.rate = float(rate)
        self.name = name
        self.commanded = float(commanded)
        self.clock = ArrivalClock(rate)
        self.clock.start(commanded)
        self.latency = latency
        self.exact_start = exact_start
        self.device_start: Optional[float] = None
        self.timescale_uncertainty = math.inf
        self.correction: Optional[Correction] = None
        self._lock = threading.Lock()
        #: local times the blocks were given with and the indexes of their first samples (sorted by
        #: time: found by bisection, every edge of a sync signal asks)
        self._starts: list[float] = []
        self._firsts: list[int] = []

    # -------------------------------------------------------------- input
    def arrived(self, last_index: int, at: Optional[float] = None) -> None:
        with self._lock:
            self.clock.add(last_index, time.monotonic() if at is None else at)

    def stamped(self, device_start: float, timescale: str, accuracy: float = 0.0) -> bool:
        """The device stamped its first sample at ``device_start`` of ``timescale``; used when this
        computer's clock follows a time scale too."""
        host = host_timescale_accuracy()
        if host is None:
            return False
        self.device_start = timescale_to_local(float(device_start), timescale)
        self.timescale_uncertainty = host + float(accuracy)
        return True

    def correct(self, correction: Optional[Correction]) -> None:
        with self._lock:
            self.correction = correction

    # ------------------------------------------------------------- output
    def raw(self, index: float) -> Estimate:
        """The time of the sample ``index`` without a sync signal."""
        if self.exact_start is not None:
            return Estimate(self.exact_start + index / self.rate, 0.0, METHOD_SIMULATED)
        if self.device_start is not None:
            period = self.clock.period if self.clock.ready else 1.0 / self.rate
            return Estimate(self.device_start + index * period, self.timescale_uncertainty, METHOD_TIMESCALE)
        with self._lock:
            if not self.clock.ready:
                return Estimate(self.commanded + index / self.rate, math.inf, METHOD_ARRIVAL)
            return self.clock.estimate(index, self.latency)

    def time_of(self, index: float) -> float:
        """Local time of the sample ``index`` (corrected by a sync signal when there is one)."""
        raw = self.raw(index).time
        correction = self.correction
        return correction.apply(raw) if correction is not None else raw

    def given(self, local_start: float, first_index: int) -> None:
        """A block starting at ``first_index`` was handed on at ``local_start`` (see :meth:`index_of`)."""
        with self._lock:
            position = bisect.bisect_right(self._starts, float(local_start))
            self._starts.insert(position, float(local_start))
            self._firsts.insert(position, int(first_index))
            if len(self._starts) > 2 * HISTORY:
                del self._starts[:HISTORY], self._firsts[:HISTORY]

    def index_of(self, local: float) -> Optional[float]:
        """The sample index at the local time ``local`` of a block that was handed on."""
        with self._lock:
            position = bisect.bisect_right(self._starts, local + 0.5 / self.rate) - 1
            if position < 0:
                return None
            start, first = self._starts[position], self._firsts[position]
        return first + (local - start) * self.rate

    def state(self) -> TimingState:
        raw = self.raw(0)
        jitter = self.clock.jitter() if self.clock.ready else 0.0
        drift = self.clock.drift if self.clock.ready else 0.0
        correction = self.correction
        if correction is not None:
            return TimingState(METHOD_SIGNAL, correction.uncertainty, self.latency, drift + correction.drift, jitter,
                               correction.reference)
        return TimingState(raw.method, raw.uncertainty, self.latency, drift, jitter)


class InstrumentTiming:
    """The time of the samples of one instrument: its current acquisition (thread safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current: Optional[Acquisition] = None
        self.last_state: Optional[TimingState] = None

    def begin(self, acquisition: Acquisition) -> Acquisition:
        with self._lock:
            self.current = acquisition
        return acquisition

    def state(self) -> Optional[TimingState]:
        acquisition = self.current
        if acquisition is None:
            return self.last_state
        self.last_state = acquisition.state()
        return self.last_state


def timing_of(instrument: Any) -> InstrumentTiming:
    """The timing of ``instrument`` (made when first asked for)."""
    timing = getattr(instrument, "timing", None)
    if timing is None:
        timing = InstrumentTiming()
        instrument.timing = timing
    return timing


# ------------------------------------------------------------- sync signals
def edges(values: np.ndarray, start: float, rate: float, threshold: Optional[float] = None
          ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(times, levels, indexes)`` of the edges of evenly sampled ``values`` (digital, or analog
    over ``threshold``: by default the middle between its lowest and highest value)."""
    values = np.asarray(values)
    if values.dtype.kind == "f":
        if threshold is None:
            low, high = float(np.min(values)) if len(values) else 0.0, float(np.max(values)) if len(values) else 0.0
            threshold = (low + high) / 2
        levels = (values >= threshold).astype(np.int8)
    else:
        levels = (values != 0).astype(np.int8)
    index = np.nonzero(levels[1:] != levels[:-1])[0] + 1
    return start + index / float(rate), levels[index], index


def match(reference: np.ndarray, times: np.ndarray, max_shift: float,
          tolerance: Optional[float] = None) -> list[tuple[float, float]]:
    """Pairs ``(time, reference time)`` of the edges of one signal recorded twice: the shift that
    makes most edges meet (irregular edges: no ambiguity), then the nearest reference edge of every
    edge within ``tolerance`` (default: a fifth of the shortest distance of the reference edges)."""
    from .hub import reference_offset

    reference = np.sort(np.asarray(reference, dtype=np.float64))
    times = np.sort(np.asarray(times, dtype=np.float64))
    if len(reference) < 2 or len(times) < 2:
        return []
    gaps = np.diff(reference)
    tolerance = tolerance if tolerance is not None else max(0.2 * float(np.min(gaps[gaps > 0])) if np.any(gaps > 0)
                                                            else 1e-3, 1e-9)
    shift = reference_offset(reference, times, max_shift, minimum_edges=2, tolerance=tolerance)
    if shift is None:
        return []
    moved = times + shift
    position = np.clip(np.searchsorted(reference, moved), 1, len(reference) - 1)
    before, after = reference[position - 1], reference[position]
    nearest = np.where(np.abs(before - moved) <= np.abs(after - moved), before, after)
    close = np.abs(nearest - moved) <= tolerance
    return [(float(time_), float(ref)) for time_, ref in zip(times[close], nearest[close])]


def fit_correction(pairs: list[tuple[float, float]], fit_drift: bool = True, reference: str = "",
                   base_uncertainty: float = 0.0) -> Correction:
    """The :class:`Correction` that moves the times of ``pairs`` onto their reference times; without
    ``fit_drift`` (a clock shared with the reference) only the offset."""
    if not pairs:
        raise ValueError("no matched edges")
    at = pairs[-1][0]
    differences = [(raw, ref - raw) for raw, ref in pairs]
    if fit_drift and len(pairs) >= 3 and pairs[-1][0] - pairs[0][0] > 0:
        offset, drift, scatter = fit_line(differences, at)
    else:
        offsets = [difference for _raw, difference in differences]
        offset, drift = sum(offsets) / len(offsets), 0.0
        scatter = math.sqrt(sum((value - offset) ** 2 for value in offsets) / max(len(offsets) - 1, 1)) \
            if len(offsets) > 1 else 0.0
    return Correction(offset, drift, at, 2 * scatter + base_uncertainty, reference, len(pairs))
