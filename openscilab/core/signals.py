# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Typed signals: what flows between instruments, nodes and views.

Every signal has a type (the type of a port in the flow graph), a time base and a unit:

=========== ======================================================== ===========================
Type        Content                                                  Time base
=========== ======================================================== ===========================
``Digital`` one logic line, ``uint8`` 0/1 per sample                 rate or timestamps
``Analog``  one analog line, ``float64`` per sample, with a unit     rate or timestamps
``Scalar``  one number with a unit (a measurement, a set value)      the time it is valid from
``Bool``    one truth value                                          the time it is valid from
``Event``   things that happened at points in time, with data       timestamps
``States``  values taken on the edges of a clock (state mode)        timestamps (may be unknown)
``Capture`` several digital and analog lines recorded together       rate
``Table``   rows of named columns                                    –
=========== ======================================================== ===========================

Streams arrive in blocks; :meth:`Signal.append` adds a block to a signal (the time bases must
continue). Conversions (:data:`CONVERSIONS`) turn one type into another; the flow editor offers
them when two ports do not fit.

The module has no Qt dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, ClassVar, Optional, Sequence

import numpy as np

DIGITAL = "Digital"
ANALOG = "Analog"
SCALAR = "Scalar"
BOOL = "Bool"
EVENT = "Event"
STATES = "States"
CAPTURE = "Capture"
TABLE = "Table"
#: A port that takes every type.
ANY = "Any"
#: An instrument: the wire from a device node to the nodes that use the device (not a value that
#: flows; only wires between device ports fit)
DEVICE = "Device"
SIGNAL_TYPES = (DIGITAL, ANALOG, SCALAR, BOOL, EVENT, STATES, CAPTURE, TABLE)


class SignalError(ValueError):
    """Signals that do not fit together (different rates, units, time bases)."""


# ----------------------------------------------------------------- time base
@dataclass(frozen=True)
class TimeBase:
    """When the samples of a signal were taken.

    Either a constant ``rate`` (Hz) from ``start`` (seconds), or explicit ``timestamps`` (seconds,
    one per sample). A time base with neither is *unknown* (a state capture without time stamps).
    """

    rate: Optional[float] = None
    start: float = 0.0
    timestamps: Optional[np.ndarray] = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if self.rate is not None and self.rate <= 0:
            raise SignalError(f"a sample rate must be positive, not {self.rate}")
        if self.rate is not None and self.timestamps is not None:
            raise SignalError("a time base has a rate or time stamps, not both")
        if self.timestamps is not None:
            object.__setattr__(self, "timestamps", np.asarray(self.timestamps, dtype=np.float64))

    @staticmethod
    def uniform(rate: float, start: float = 0.0) -> "TimeBase":
        return TimeBase(rate=float(rate), start=float(start))

    @staticmethod
    def stamped(timestamps: Sequence[float]) -> "TimeBase":
        return TimeBase(timestamps=np.asarray(timestamps, dtype=np.float64))

    @property
    def is_uniform(self) -> bool:
        return self.rate is not None

    @property
    def is_known(self) -> bool:
        return self.rate is not None or self.timestamps is not None

    @property
    def period(self) -> Optional[float]:
        return 1.0 / self.rate if self.rate else None

    def times(self, count: int) -> np.ndarray:
        """Time of each of ``count`` samples (seconds)."""
        if self.rate is not None:
            return self.start + np.arange(count, dtype=np.float64) / self.rate
        if self.timestamps is not None:
            if len(self.timestamps) < count:
                raise SignalError(f"{len(self.timestamps)} time stamps for {count} samples")
            return self.timestamps[:count]
        raise SignalError("the time base is unknown")

    def time_of(self, index: int) -> float:
        if self.rate is not None:
            return self.start + index / self.rate
        if self.timestamps is not None:
            return float(self.timestamps[index])
        raise SignalError("the time base is unknown")

    def index_of(self, time: float) -> int:
        """Index of the last sample at or before ``time``."""
        if self.rate is not None:
            return int(np.floor((time - self.start) * self.rate + 1e-9))
        if self.timestamps is not None:
            return int(np.searchsorted(self.timestamps, time, side="right")) - 1
        raise SignalError("the time base is unknown")

    def end(self, count: int) -> float:
        """Time just after the last of ``count`` samples (start of the next block)."""
        if self.rate is not None:
            return self.start + count / self.rate
        if self.timestamps is not None and count:
            return float(self.timestamps[count - 1])
        return self.start

    def shifted(self, seconds: float) -> "TimeBase":
        if self.timestamps is not None:
            return TimeBase(timestamps=self.timestamps + seconds)
        return replace(self, start=self.start + seconds)

    def sliced(self, first: int, last: int) -> "TimeBase":
        if self.rate is not None:
            return TimeBase(rate=self.rate, start=self.start + first / self.rate)
        if self.timestamps is not None:
            return TimeBase(timestamps=self.timestamps[first:last])
        return self

    def continued_by(self, other: "TimeBase", count: int) -> "TimeBase":
        """Time base of ``count`` samples on this one followed by the samples of ``other``."""
        if self.rate is not None:
            if other.rate is None or abs(other.rate - self.rate) > 1e-9 * self.rate:
                raise SignalError(f"a block of {other.rate} Hz does not continue {self.rate} Hz")
            expected = self.end(count)
            if abs(other.start - expected) > 0.5 / self.rate:
                raise SignalError(f"the block starts at {other.start} s, expected {expected} s")
            return self
        if self.timestamps is not None:
            if other.timestamps is None:
                raise SignalError("a block with a rate does not continue time stamps")
            return TimeBase(timestamps=np.concatenate([self.timestamps[:count], other.timestamps]))
        return self


# ------------------------------------------------------------------- signals
@dataclass
class Signal:
    """Base of all signal types."""

    type_name: ClassVar[str] = ANY

    name: str = ""
    unit: str = ""

    def append(self, block: "Signal") -> "Signal":
        raise SignalError(f"{self.type_name} signals do not take blocks")

    def describe(self) -> str:
        return f"{self.type_name} {self.name}".strip()


@dataclass
class _Sampled(Signal):
    values: np.ndarray = field(default_factory=lambda: np.zeros(0))
    time: TimeBase = field(default_factory=TimeBase)

    dtype: ClassVar[Any] = np.float64

    def __post_init__(self) -> None:
        self.values = np.asarray(self.values, dtype=self.dtype)
        if self.time.timestamps is not None and len(self.time.timestamps) != len(self.values):
            raise SignalError(f"{len(self.time.timestamps)} time stamps for {len(self.values)} samples")

    def __len__(self) -> int:
        return len(self.values)

    @property
    def rate(self) -> Optional[float]:
        return self.time.rate

    def times(self) -> np.ndarray:
        return self.time.times(len(self.values))

    @property
    def duration(self) -> float:
        if not len(self.values):
            return 0.0
        return self.time.end(len(self.values)) - self.time.time_of(0)

    def append(self, block: "Signal") -> "_Sampled":
        if type(block) is not type(self):
            raise SignalError(f"a {block.type_name} block does not continue a {self.type_name} signal")
        if block.unit != self.unit:
            raise SignalError(f"a block in {block.unit or 'no unit'} does not continue {self.unit or 'no unit'}")
        if not len(self.values):
            self.values = block.values.copy()
            self.time = block.time
            return self
        self.time = self.time.continued_by(block.time, len(self.values))
        self.values = np.concatenate([self.values, block.values])
        return self

    def window(self, start: float, stop: float) -> "_Sampled":
        """The samples from ``start`` to ``stop`` (seconds)."""
        times = self.times()
        first = int(np.searchsorted(times, start, side="left"))
        last = int(np.searchsorted(times, stop, side="left"))
        return replace(self, values=self.values[first:last], time=self.time.sliced(first, last))

    def value_at(self, time: float):
        index = self.time.index_of(time)
        if index < 0 or index >= len(self.values):
            raise SignalError(f"no sample at {time} s")
        return self.values[index]


@dataclass
class Digital(_Sampled):
    """One logic line."""

    type_name: ClassVar[str] = DIGITAL
    dtype: ClassVar[Any] = np.uint8

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.values.size and self.values.max(initial=0) > 1:
            self.values = (self.values != 0).astype(np.uint8)

    def edges(self, kind: str = "both") -> "Event":
        """The rising, falling or both edges as events (data: 1 rising, 0 falling)."""
        values = self.values.astype(np.int8)
        change = np.diff(values)
        if kind == "rising":
            index = np.flatnonzero(change > 0) + 1
        elif kind == "falling":
            index = np.flatnonzero(change < 0) + 1
        else:
            index = np.flatnonzero(change != 0) + 1
        times = self.time.times(len(values))[index] if len(values) else np.zeros(0)
        return Event(name=self.name, times=times, data=[int(self.values[i]) for i in index])

    def to_analog(self, low: float = 0.0, high: float = 3.3, unit: str = "V") -> "Analog":
        return Analog(name=self.name, unit=unit, values=np.where(self.values != 0, high, low),
                      time=self.time)

    def high_share(self) -> "Scalar":
        """Share of the samples at 1 (duty cycle of a uniform signal)."""
        share = float(self.values.mean()) if len(self.values) else 0.0
        return Scalar(name=self.name, unit="", value=share, at=self.time.time_of(0) if len(self.values) else 0.0)


@dataclass
class Analog(_Sampled):
    """One analog line (volts by default)."""

    type_name: ClassVar[str] = ANALOG

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.unit:
            self.unit = "V"

    def to_digital(self, threshold: float, hysteresis: float = 0.0, initial: int = 0) -> Digital:
        """1 above ``threshold + hysteresis/2``, 0 below ``threshold - hysteresis/2``."""
        if hysteresis <= 0:
            return Digital(name=self.name, values=(self.values >= threshold).astype(np.uint8), time=self.time)
        high = threshold + hysteresis / 2
        low = threshold - hysteresis / 2
        # Hysteresis needs the previous state: between the thresholds a sample has the level of
        # the last sample that was above the upper or below the lower one. For every sample
        # that is the index of the last decided sample up to it (a running maximum), without a
        # loop (a noisy signal decides at almost every sample).
        above = self.values >= high
        decided = above | (self.values <= low)
        last = np.maximum.accumulate(np.where(decided, np.arange(len(decided)), -1))
        result = np.where(last >= 0, above[np.maximum(last, 0)], bool(initial)).astype(np.uint8)
        return Digital(name=self.name, values=result, time=self.time)

    def mean(self) -> "Scalar":
        return self._scalar(float(np.mean(self.values)) if len(self.values) else float("nan"))

    def minimum(self) -> "Scalar":
        return self._scalar(float(np.min(self.values)) if len(self.values) else float("nan"))

    def maximum(self) -> "Scalar":
        return self._scalar(float(np.max(self.values)) if len(self.values) else float("nan"))

    def rms(self) -> "Scalar":
        return self._scalar(float(np.sqrt(np.mean(np.square(self.values)))) if len(self.values) else float("nan"))

    def _scalar(self, value: float) -> "Scalar":
        at = self.time.end(len(self.values)) if len(self.values) and self.time.is_known else 0.0
        return Scalar(name=self.name, unit=self.unit, value=value, at=at)


@dataclass
class Scalar(Signal):
    """One number with a unit, valid from ``at`` (seconds)."""

    type_name: ClassVar[str] = SCALAR

    value: float = 0.0
    at: float = 0.0

    def __float__(self) -> float:
        return float(self.value)

    def to_bool(self, threshold: float = 0.5) -> "Bool":
        return Bool(name=self.name, value=self.value >= threshold, at=self.at)


@dataclass
class Bool(Signal):
    """A truth value, valid from ``at`` (seconds)."""

    type_name: ClassVar[str] = BOOL

    value: bool = False
    at: float = 0.0

    def __bool__(self) -> bool:
        return bool(self.value)

    def to_scalar(self) -> Scalar:
        return Scalar(name=self.name, value=1.0 if self.value else 0.0, at=self.at)


@dataclass
class Event(Signal):
    """Points in time with data (decoder output, edges, button presses, ...)."""

    type_name: ClassVar[str] = EVENT

    times: np.ndarray = field(default_factory=lambda: np.zeros(0))
    data: list = field(default_factory=list)

    def __post_init__(self) -> None:
        self.times = np.asarray(self.times, dtype=np.float64)
        if not self.data:
            self.data = [None] * len(self.times)
        if len(self.data) != len(self.times):
            raise SignalError(f"{len(self.data)} data entries for {len(self.times)} events")

    def __len__(self) -> int:
        return len(self.times)

    def __iter__(self):
        return iter(zip(self.times.tolist(), self.data))

    def append(self, block: Signal) -> "Event":
        if not isinstance(block, Event):
            raise SignalError(f"a {block.type_name} block does not continue an Event signal")
        if len(self.times) and len(block.times) and block.times[0] < self.times[-1]:
            raise SignalError("events have to arrive in the order of time")
        self.times = np.concatenate([self.times, block.times])
        self.data = list(self.data) + list(block.data)
        return self

    def count(self) -> Scalar:
        return Scalar(name=self.name, value=float(len(self.times)), at=float(self.times[-1]) if len(self.times) else 0.0)

    def to_table(self, column: str = "data") -> "Table":
        return Table(name=self.name, columns={"time": list(self.times.tolist()), column: list(self.data)})


@dataclass
class States(Signal):
    """Values taken on the edges of a clock; ``times`` (seconds) when the device stamps them."""

    type_name: ClassVar[str] = STATES

    values: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.uint32))
    times: Optional[np.ndarray] = None
    #: channels of the bits of ``values`` (bit 0 = first)
    channels: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.values = np.asarray(self.values, dtype=np.uint32)
        if self.times is not None:
            self.times = np.asarray(self.times, dtype=np.float64)
            if len(self.times) != len(self.values):
                raise SignalError(f"{len(self.times)} time stamps for {len(self.values)} states")

    def __len__(self) -> int:
        return len(self.values)

    def append(self, block: Signal) -> "States":
        if not isinstance(block, States):
            raise SignalError(f"a {block.type_name} block does not continue a States signal")
        if (self.times is None) != (block.times is None) and len(self.values):
            raise SignalError("states with and without time stamps do not mix")
        self.values = np.concatenate([self.values, block.values])
        if block.times is not None:
            self.times = block.times.copy() if self.times is None else np.concatenate([self.times, block.times])
        if not self.channels:
            self.channels = list(block.channels)
        return self

    def bit(self, index: int) -> Digital:
        """One channel as a digital signal over the states (time base: the time stamps)."""
        values = ((self.values >> index) & 1).astype(np.uint8)
        time = TimeBase.stamped(self.times) if self.times is not None else TimeBase()
        name = self.channels[index] if index < len(self.channels) else f"bit {index}"
        return Digital(name=name, values=values, time=time)


@dataclass
class Capture(Signal):
    """Lines recorded together: digital and analog channels by name, on one rate."""

    type_name: ClassVar[str] = CAPTURE

    rate: float = 1.0
    start: float = 0.0
    digital: dict[str, np.ndarray] = field(default_factory=dict)
    analog: dict[str, np.ndarray] = field(default_factory=dict)
    analog_units: dict[str, str] = field(default_factory=dict)
    #: index of the trigger sample
    trigger: int = 0
    #: the session the capture came from (when it came from an analyzer)
    session: Any = None

    @property
    def channels(self) -> list[str]:
        return list(self.digital) + list(self.analog)

    @property
    def sample_count(self) -> int:
        lengths = [len(values) for values in list(self.digital.values()) + list(self.analog.values())]
        return max(lengths, default=0)

    def time(self) -> TimeBase:
        return TimeBase.uniform(self.rate, self.start)

    def channel(self, name: str) -> Signal:
        """The line ``name`` as a :class:`Digital` or :class:`Analog` signal."""
        if name in self.digital:
            return Digital(name=name, values=self.digital[name], time=self.time())
        if name in self.analog:
            return Analog(name=name, unit=self.analog_units.get(name, "V"), values=self.analog[name], time=self.time())
        raise KeyError(f"the capture has no channel {name!r} (channels: {', '.join(self.channels)})")

    def append(self, block: Signal) -> "Capture":
        if not isinstance(block, Capture):
            raise SignalError(f"a {block.type_name} block does not continue a Capture")
        if block.rate != self.rate:
            raise SignalError(f"a block of {block.rate} Hz does not continue {self.rate} Hz")
        for target, source in ((self.digital, block.digital), (self.analog, block.analog)):
            for name, values in source.items():
                target[name] = np.concatenate([target[name], values]) if name in target else values.copy()
        return self

    @staticmethod
    def from_session(session) -> "Capture":
        """A capture of a :class:`~openscilab.driver.models.CaptureSession`."""
        digital = {}
        for channel in session.capture_channels:
            if channel.samples is not None:
                digital[channel.channel_name or f"Channel {channel.channel_number + 1}"] = np.asarray(
                    channel.samples, dtype=np.uint8)
        analog, units = {}, {}
        for channel in getattr(session, "analog_channels", None) or []:
            analog[channel.display_name] = channel.volts()
            units[channel.display_name] = channel.unit
        return Capture(name="capture", rate=float(session.frequency), digital=digital, analog=analog,
                       analog_units=units, trigger=session.pre_trigger_samples, session=session)

    def to_session(self):
        """A :class:`~openscilab.driver.models.CaptureSession` with the digital lines."""
        from ..driver.models import AnalyzerChannel, CaptureSession

        if self.session is not None:
            return self.session
        count = self.sample_count
        session = CaptureSession(frequency=int(self.rate), pre_trigger_samples=self.trigger,
                                 post_trigger_samples=max(count - self.trigger, 0))
        session.capture_channels = [
            AnalyzerChannel(channel_number=index, channel_name=name, samples=np.asarray(values, dtype=np.uint8))
            for index, (name, values) in enumerate(self.digital.items())
        ]
        from ..driver.models import AnalogChannel

        session.analog_channels = [
            AnalogChannel.from_volts(values, channel_number=index, channel_name=name,
                                     unit=self.analog_units.get(name, "V"))
            for index, (name, values) in enumerate(self.analog.items())
        ]
        if not self.digital:
            session.post_trigger_samples = max(count - self.trigger, 0)
        return session


@dataclass
class Table(Signal):
    """Rows of named columns (measurement results, decoder output, a sweep)."""

    type_name: ClassVar[str] = TABLE

    columns: dict[str, list] = field(default_factory=dict)

    def __post_init__(self) -> None:
        lengths = {len(values) for values in self.columns.values()}
        if len(lengths) > 1:
            raise SignalError(f"the columns have different lengths: {sorted(lengths)}")
        self.columns = {name: list(values) for name, values in self.columns.items()}

    def __len__(self) -> int:
        return len(next(iter(self.columns.values()), []))

    @staticmethod
    def from_rows(rows: Sequence[dict], name: str = "") -> "Table":
        names: list[str] = []
        for row in rows:
            for key in row:
                if key not in names:
                    names.append(key)
        return Table(name=name, columns={key: [row.get(key) for row in rows] for key in names})

    def rows(self) -> list[dict]:
        names = list(self.columns)
        return [dict(zip(names, values)) for values in zip(*self.columns.values())]

    def add_row(self, row: dict) -> None:
        count = len(self)
        for key in row:
            if key not in self.columns:
                self.columns[key] = [None] * count
        for key, values in self.columns.items():
            values.append(row.get(key))

    def append(self, block: Signal) -> "Table":
        if not isinstance(block, Table):
            raise SignalError(f"a {block.type_name} block does not continue a Table")
        for row in block.rows():
            self.add_row(row)
        return self

    def column(self, name: str) -> np.ndarray:
        return np.asarray(self.columns[name])


TYPES: dict[str, type] = {cls.type_name: cls for cls in (Digital, Analog, Scalar, Bool, Event, States, Capture, Table)}


# --------------------------------------------------------------- conversions
@dataclass(frozen=True)
class Conversion:
    """Turns a signal of ``source`` type into ``target`` type; ``node`` is the node that does it."""

    source: str
    target: str
    node: str
    function: Callable[..., Signal]
    description: str = ""
    #: the parameters the node is inserted with (it does what ``function`` does)
    params: dict = field(default_factory=dict)


CONVERSIONS: list[Conversion] = [
    Conversion(ANALOG, DIGITAL, "dsp.threshold", lambda s, threshold=1.65, hysteresis=0.0: s.to_digital(threshold, hysteresis),
               "1 above a threshold (with hysteresis)"),
    Conversion(DIGITAL, ANALOG, "convert.to_analog", lambda s, low=0.0, high=3.3: s.to_analog(low, high),
               "logic levels as volts"),
    Conversion(DIGITAL, EVENT, "convert.edges", lambda s, kind="both": s.edges(kind), "the edges as events"),
    Conversion(ANALOG, SCALAR, "measure.mean", lambda s: s.mean(), "mean value"),
    Conversion(DIGITAL, SCALAR, "measure.duty", lambda s: s.high_share(), "share of the time at 1"),
    Conversion(SCALAR, BOOL, "control.compare", lambda s, threshold=0.5: s.to_bool(threshold), "at or above a threshold",
               {"op": ">=", "value": 0.5}),
    Conversion(BOOL, SCALAR, "convert.to_scalar", lambda s: s.to_scalar(), "1 or 0"),
    Conversion(EVENT, TABLE, "data.table", lambda s: s.to_table(), "one row per event"),
    Conversion(EVENT, SCALAR, "control.counter", lambda s: s.count(), "number of events"),
    Conversion(STATES, DIGITAL, "convert.state_bit", lambda s, bit=0: s.bit(bit), "one bit of the states"),
    Conversion(CAPTURE, DIGITAL, "convert.channel", lambda s, channel="": s.channel(channel or next(iter(s.digital))),
               "one digital channel", {"kind": "digital"}),
    Conversion(CAPTURE, ANALOG, "convert.channel", lambda s, channel="": s.channel(channel or next(iter(s.analog))),
               "one analog channel", {"kind": "analog"}),
]


def compatible(source: str, target: str) -> bool:
    """A port of type ``source`` can be wired to a port of type ``target``."""
    if DEVICE in (source, target):
        return source == target
    return source == target or ANY in (source, target)


def conversion(source: str, target: str) -> Optional[Conversion]:
    """The conversion from ``source`` to ``target`` type, ``None`` when there is none."""
    for candidate in CONVERSIONS:
        if candidate.source == source and candidate.target == target:
            return candidate
    return None


def convert(signal: Signal, target: str, **options) -> Signal:
    """``signal`` as type ``target`` (raises :class:`SignalError` without a conversion)."""
    if compatible(signal.type_name, target):
        return signal
    found = conversion(signal.type_name, target)
    if found is None:
        raise SignalError(f"a {signal.type_name} signal cannot be turned into {target}")
    return found.function(signal, **options)
