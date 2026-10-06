# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The data of a run: what every output of a flow sent, for the graph and the data views.

:class:`RunData` keeps per ``node.port`` the latest value, the numbers over time (a measurement
per sweep step, the monitor's readings), the captures and sampled signals with their place in
time, and the events. :meth:`RunData.to_session` lays all of it – or the ports of one node – on
one time axis as a :class:`~openscilab.driver.models.CaptureSession`, which a data view shows:
the channels of every capture, the digital and analog signals, and the numbers as analog
channels that step from value to value.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import numpy as np

from ..core import signals

#: numbers kept per output (the oldest go first)
HISTORY = 100_000
#: captures and sampled signals kept per output
BLOCKS = 200
#: samples per channel of a session of the whole run at most (the rate is lowered to fit)
MAX_SAMPLES = 4_000_000
#: the rate of a session that holds numbers only (steps of measurements)
NUMBERS_RATE = 1000.0


def number_of(value: Any) -> Optional[float]:
    """The number a value stands for (``None`` for values that are no single number)."""
    if isinstance(value, (bool, np.bool_)):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float, np.number)):
        return float(value)
    if isinstance(value, (signals.Scalar, signals.Bool)):
        return float(value.value)
    return None


@dataclass
class PortData:
    """What one output sent during a run."""

    node: str
    port: str
    latest: Any = None
    count: int = 0
    #: (time, number) of the values that are numbers
    numbers: deque = field(default_factory=lambda: deque(maxlen=HISTORY))
    unit: str = ""
    #: every number was a truth value (shown as a digital line)
    boolean: bool = True
    #: captures, digital and analog signals (each knows its place in time)
    blocks: deque = field(default_factory=lambda: deque(maxlen=BLOCKS))

    @property
    def key(self) -> str:
        return f"{self.node}.{self.port}"

    @property
    def kind(self) -> str:
        """``signal`` (captures, sampled lines), ``number``, ``table``, ``event`` or ``value``."""
        if self.blocks:
            return "signal"
        if self.numbers:
            return "number"
        if isinstance(self.latest, signals.Table):
            return "table"
        if isinstance(self.latest, signals.Event):
            return "event"
        return "value"

    @property
    def viewable(self) -> bool:
        """Whether a data view can show it (as channels)."""
        return self.kind in ("signal", "number")


def _block_start(block) -> Optional[float]:
    if isinstance(block, signals.Capture):
        return float(block.start)
    if isinstance(block, (signals.Digital, signals.Analog)) and len(block) and block.time.is_known:
        return float(block.time.time_of(0))
    return None


def _block_rate(block) -> Optional[float]:
    if isinstance(block, signals.Capture):
        return float(block.rate) if block.rate else None
    if isinstance(block, (signals.Digital, signals.Analog)):
        return float(block.rate) if block.rate else None
    return None


def _block_lines(key: str, block) -> list[tuple[str, str, np.ndarray, str]]:
    """``(channel name, "digital"/"analog", samples, unit)`` of a block."""
    if isinstance(block, signals.Capture):
        node = key.split(".", 1)[0]
        lines = [(f"{node}.{name}", "digital", np.asarray(values), "") for name, values in block.digital.items()]
        lines += [(f"{node}.{name}", "analog", np.asarray(values, dtype=np.float64), block.analog_units.get(name, "V"))
                  for name, values in block.analog.items()]
        return lines
    if isinstance(block, signals.Digital):
        return [(key, "digital", block.values, "")]
    if isinstance(block, signals.Analog):
        return [(key, "analog", block.values, block.unit or "V")]
    return []


class RunData:
    """Collects the values of a run (thread safe: the engine's values arrive in batches)."""

    def __init__(self) -> None:
        self._ports: dict[tuple[str, str], PortData] = {}
        self._lock = threading.Lock()
        #: counts every change (the views ask whether there is something new)
        self.version = 0

    def clear(self) -> None:
        with self._lock:
            self._ports.clear()
            self.version += 1

    def add(self, node: str, port: str, values: Iterable, times: Optional[Iterable[float]] = None) -> None:
        """Values ``node.port`` sent (oldest first), with the flow times they were sent at."""
        values = list(values)
        if not values:
            return
        times = list(times) if times is not None else [None] * len(values)
        with self._lock:
            data = self._ports.get((node, port))
            if data is None:
                data = self._ports[(node, port)] = PortData(node, port)
            for value, time in zip(values, times):
                data.latest = value
                data.count += 1
                if isinstance(value, (signals.Capture, signals.Digital, signals.Analog)):
                    if _block_start(value) is None and time is not None:
                        value = _placed(value, time)
                    data.blocks.append(value)
                    continue
                number = number_of(value)
                if number is not None:
                    at = getattr(value, "at", None)
                    at = float(at) if at is not None and (time is None or at) else time
                    data.numbers.append((float(at) if at is not None else float(len(data.numbers)), number))
                    data.boolean = data.boolean and isinstance(value, (bool, np.bool_, signals.Bool))
                    if getattr(value, "unit", ""):
                        data.unit = value.unit
            self.version += 1

    # ------------------------------------------------------------- reading
    def ports(self, node: Optional[str] = None) -> list[PortData]:
        with self._lock:
            return [data for (owner, _port), data in self._ports.items() if node is None or owner == node]

    def get(self, node: str, port: str) -> Optional[PortData]:
        with self._lock:
            return self._ports.get((node, port))

    def __bool__(self) -> bool:
        return bool(self._ports)

    def to_session(self, keys: Optional[Iterable[str]] = None, max_samples: int = MAX_SAMPLES):
        """The signals and numbers of ``keys`` (``node.port``; all when ``None``) on one time axis;
        ``None`` when none of them can be shown as channels."""
        from ..driver.models import AnalogChannel, AnalyzerChannel, CaptureSession, TriggerType

        wanted = set(keys) if keys is not None else None
        chosen = [data for data in self.ports() if data.viewable and (wanted is None or data.key in wanted)]
        if not chosen:
            return None
        starts, ends, rates = [], [], []
        for data in chosen:
            for block in list(data.blocks):
                start, rate = _block_start(block), _block_rate(block)
                if start is None or not rate:
                    continue
                count = block.sample_count if isinstance(block, signals.Capture) else len(block)
                starts.append(start)
                ends.append(start + count / rate)
                rates.append(rate)
            if data.numbers:
                starts.append(data.numbers[0][0])
                ends.append(data.numbers[-1][0])
        if not starts:
            return None
        begin, end = min(starts), max(ends)
        rate = max(rates) if rates else NUMBERS_RATE
        span = max(end - begin, 1.0 / rate)
        if span * rate > max_samples:
            rate = max_samples / span
        count = max(math.ceil(span * rate) + 1, 2)
        grid = begin + np.arange(count) / rate

        digital: dict[str, np.ndarray] = {}
        analog: dict[str, tuple[np.ndarray, np.ndarray, str]] = {}
        for data in chosen:
            for block in list(data.blocks):
                start, block_rate = _block_start(block), _block_rate(block)
                if start is None or not block_rate:
                    continue
                for name, kind, values, unit in _block_lines(data.key, block):
                    if not len(values):
                        continue
                    # the grid samples that fall into the block, read from the nearest sample of it
                    first = max(math.ceil((start - begin) * rate - 1e-9), 0)
                    last = min(math.floor((start + len(values) / block_rate - begin) * rate - 1e-9), count - 1)
                    if last < first:
                        if first >= count:
                            continue
                        last = first  # shorter than a sample of the axis: it still shows
                    index = np.clip(np.floor((grid[first:last + 1] - start) * block_rate + 1e-6).astype(np.int64),
                                    0, len(values) - 1)
                    if kind == "digital":
                        line = digital.setdefault(name, np.zeros(count, dtype=np.uint8))
                        line[first:last + 1] = (values[index] != 0)
                    else:
                        line, known, _unit = analog.setdefault(name, (np.zeros(count), np.zeros(count, bool), unit))
                        line[first:last + 1] = values[index]
                        known[first:last + 1] = True
            if data.numbers:
                times = np.asarray([item[0] for item in data.numbers])
                numbers = np.asarray([item[1] for item in data.numbers])
                order = np.argsort(times, kind="stable")
                times, numbers = times[order], numbers[order]
                position = np.searchsorted(times, grid, side="right") - 1
                known = position >= 0
                line = np.where(known, numbers[np.clip(position, 0, len(numbers) - 1)], 0.0)
                if data.boolean:
                    digital[data.key] = (line != 0).astype(np.uint8)
                else:
                    analog[data.key] = (line, known, data.unit)

        session = CaptureSession(frequency=max(round(rate), 1), pre_trigger_samples=0, post_trigger_samples=count)
        session.trigger_type = TriggerType.IMMEDIATE  # laid together from many values: no trigger
        session.capture_channels = [AnalyzerChannel(channel_number=index, channel_name=name, samples=samples)
                                    for index, (name, samples) in enumerate(digital.items())]
        channels = []
        for index, (name, (line, known, unit)) in enumerate(analog.items()):
            if known.any():
                # where nothing was measured the line holds the nearest value (no gap to draw)
                first_known = int(np.argmax(known))
                line[:first_known] = line[first_known]
                positions = np.where(known, np.arange(count), 0)
                line = line[np.maximum.accumulate(positions)]
            channels.append(AnalogChannel.from_volts(line, channel_number=index, channel_name=name, unit=unit))
        session.analog_channels = channels
        return session


def _placed(block, time: float):
    """A sampled signal without a known start, placed at ``time`` (when it was sent)."""
    if isinstance(block, (signals.Digital, signals.Analog)) and block.rate:
        from dataclasses import replace

        return replace(block, time=signals.TimeBase.uniform(block.rate, time))
    return block
