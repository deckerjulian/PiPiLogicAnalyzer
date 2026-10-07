# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Waveforms for generator outputs.

A :class:`Waveform` is either *analog* (volts over time: a standard shape, or arbitrary points
from a formula, a CSV file or a capture) or a digital *pattern* (levels per pin on a sample rate:
SDL per pin, from a capture, or protocol blocks for UART, SPI and I²C). Modulation: a frequency
sweep, bursts, and a number of repeats.

A waveform is a function of time from its start: :meth:`Waveform.analog` and
:meth:`Waveform.digital` give the values at any times, which is all an output (or the simulator)
needs. :func:`problems` checks a waveform against the limits of an output
(:class:`~openscilab.core.instrument.OutputInfo`). Waveforms are stored as ``*.wave.yaml``.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Optional

import numpy as np

from . import units, yaml_text

SINE = "sine"
SQUARE = "square"
TRIANGLE = "triangle"
RAMP = "ramp"
PULSE = "pulse"
DC = "dc"
NOISE = "noise"
ARBITRARY = "arbitrary"
PATTERN = "pattern"
#: the standard shapes, their parameters and arbitrary points: volts over time
ANALOG_KINDS = (SINE, SQUARE, TRIANGLE, RAMP, PULSE, DC, NOISE, ARBITRARY)
KINDS = ANALOG_KINDS + (PATTERN,)

SWEEP = "sweep"
BURST = "burst"


class WaveformError(ValueError):
    pass


@dataclass
class Waveform:
    """An analog waveform or a digital pattern (see the module documentation)."""

    kind: str = SINE
    name: str = ""
    #: Hz: of the shape, or of one pass through the points of an arbitrary waveform
    frequency: float = 1000.0
    #: volts: peak (half of peak to peak) around ``offset``; the height of a pulse above it
    amplitude: float = 1.0
    offset: float = 0.0
    #: 0..1: high part of a square wave, width of a pulse per period
    duty: float = 0.5
    #: degrees
    phase: float = 0.0
    #: arbitrary: the values (volts) of one pass, played evenly over 1 / frequency
    points: Optional[np.ndarray] = None
    #: where the points came from, kept to edit and store them (formula, file)
    formula: str = ""
    source: str = ""
    #: pattern: levels per pin on ``rate``; ``sdl`` keeps the text a track was made from
    tracks: dict[str, np.ndarray] = field(default_factory=dict)
    sdl: dict[str, str] = field(default_factory=dict)
    rate: float = 1_000_000.0
    #: passes through the waveform: 0 repeats until stopped, n stops after n (holding the end)
    repeat: int = 0
    #: sweep: frequency from ``sweep_start`` to ``sweep_stop`` within ``sweep_time`` seconds
    modulation: str = ""
    sweep_start: float = 100.0
    sweep_stop: float = 10_000.0
    sweep_time: float = 1.0
    #: burst: ``burst_cycles`` periods every ``burst_period`` seconds, ``offset`` between them
    burst_cycles: int = 1
    burst_period: float = 0.01
    seed: int = 1

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise WaveformError(f"unknown waveform kind {self.kind!r} (kinds: {', '.join(KINDS)})")
        if self.points is not None:
            self.points = np.asarray(self.points, dtype=np.float64)
        self.tracks = {str(pin): np.asarray(levels, dtype=np.uint8) for pin, levels in self.tracks.items()}

    # ------------------------------------------------------------ queries
    @property
    def is_pattern(self) -> bool:
        return self.kind == PATTERN

    @property
    def pattern_length(self) -> int:
        return max((len(levels) for levels in self.tracks.values()), default=0)

    @property
    def period(self) -> float:
        """Seconds of one pass (of the pattern, of the points, of the shape)."""
        if self.is_pattern:
            return self.pattern_length / self.rate if self.rate else 0.0
        return 1.0 / self.frequency if self.frequency else math.inf

    @property
    def duration(self) -> float:
        """Seconds until the waveform holds still (``inf`` when it repeats until stopped)."""
        if self.repeat <= 0 or self.kind in (DC, NOISE):
            return math.inf if self.kind != DC else 0.0
        return self.repeat * self.period

    def voltage_range(self) -> tuple[float, float]:
        """Lowest and highest volts of an analog waveform."""
        if self.kind == ARBITRARY:
            points = self.points if self.points is not None and len(self.points) else np.zeros(1)
            return float(points.min()), float(points.max())
        if self.kind == DC:
            return self.offset, self.offset
        if self.kind == PULSE:
            return min(self.offset, self.offset + self.amplitude), max(self.offset, self.offset + self.amplitude)
        if self.kind == NOISE:  # 3 sigma
            return self.offset - 3 * abs(self.amplitude), self.offset + 3 * abs(self.amplitude)
        return self.offset - abs(self.amplitude), self.offset + abs(self.amplitude)

    def highest_frequency(self) -> float:
        if self.modulation == SWEEP:
            return max(self.sweep_start, self.sweep_stop, self.frequency if self.kind == DC else 0.0)
        return self.frequency

    def describe(self) -> str:
        if self.is_pattern:
            return (f"pattern: {len(self.tracks)} pins, {self.pattern_length} samples at "
                    f"{units.format_quantity(self.rate, 'Hz')}")
        if self.kind == DC:
            return f"DC {units.format_quantity(self.offset, 'V')}"
        text = f"{self.kind} {units.format_quantity(self.frequency, 'Hz')}"
        if self.kind != ARBITRARY:
            text += f", {units.format_quantity(self.amplitude, 'V')} peak"
        if self.offset:
            text += f", offset {units.format_quantity(self.offset, 'V')}"
        if self.modulation == SWEEP:
            text += (f", sweep {units.format_quantity(self.sweep_start, 'Hz')} to "
                     f"{units.format_quantity(self.sweep_stop, 'Hz')}")
        elif self.modulation == BURST:
            text += f", burst of {self.burst_cycles}"
        return text

    # ------------------------------------------------------------- values
    def _cycles(self, times: np.ndarray) -> np.ndarray:
        """Periods passed at ``times`` (seconds from the start), with phase and sweep."""
        if self.modulation == SWEEP and self.sweep_time > 0:
            # a linear sweep, starting again every sweep_time
            span = self.sweep_time
            passes = np.floor(times / span)
            local = times - passes * span
            f0, f1 = self.sweep_start, self.sweep_stop
            per_pass = f0 * span + (f1 - f0) * span / 2
            cycles = passes * per_pass + f0 * local + (f1 - f0) * local ** 2 / (2 * span)
        else:
            cycles = times * self.frequency
        return cycles + self.phase / 360.0

    def analog(self, times: Iterable[float] | np.ndarray) -> np.ndarray:
        """Volts at ``times`` (seconds from the start of the waveform)."""
        if self.is_pattern:
            raise WaveformError("a pattern has no voltage")
        times = np.asarray(times, dtype=np.float64)
        cycles = self._cycles(np.maximum(times, 0.0))
        if self.repeat > 0 and self.kind not in (DC, NOISE):
            cycles = np.minimum(cycles, self.repeat - 1e-9)
        fraction = cycles - np.floor(cycles)
        kind = self.kind
        if kind == SINE:
            shape = np.sin(2 * np.pi * fraction)
        elif kind == SQUARE:
            shape = np.where(fraction < self.duty, 1.0, -1.0)
        elif kind == TRIANGLE:
            shape = 1.0 - 4.0 * np.abs(fraction - 0.5)
        elif kind == RAMP:
            shape = 2.0 * fraction - 1.0
        elif kind == PULSE:
            shape = np.where(fraction < self.duty, 1.0, 0.0)
        elif kind == DC:
            shape = np.zeros(len(times))
        elif kind == NOISE:
            # the same noise for the same times: seeded by the sample position
            rng = np.random.default_rng([self.seed, int(abs(times[0]) * 1e9) if len(times) else 0])
            shape = rng.normal(0.0, 1.0, len(times))
        else:
            points = self.points if self.points is not None and len(self.points) else np.zeros(1)
            index = np.minimum((fraction * len(points)).astype(np.int64), len(points) - 1)
            values = points[index]
            return self._burst(times, cycles, values, self.offset if not len(points) else float(points[0]))
        values = self.offset + self.amplitude * shape
        return self._burst(times, cycles, values, self.offset)

    def _burst(self, times: np.ndarray, cycles: np.ndarray, values: np.ndarray, rest: float) -> np.ndarray:
        if self.modulation != BURST or self.burst_period <= 0:
            return values
        local = np.mod(times, self.burst_period)
        active = local * self.frequency < self.burst_cycles
        return np.where(active, values, rest)

    def digital(self, times: Iterable[float] | np.ndarray) -> dict[str, np.ndarray]:
        """Levels of every pin at ``times`` (seconds from the start of the pattern)."""
        if not self.is_pattern:
            raise WaveformError("an analog waveform has no pattern")
        times = np.asarray(times, dtype=np.float64)
        length = self.pattern_length
        if not length:
            return {pin: np.zeros(len(times), dtype=np.uint8) for pin in self.tracks}
        index = np.floor(np.maximum(times, 0.0) * self.rate + 1e-9).astype(np.int64)
        if self.repeat > 0:
            index = np.minimum(index, self.repeat * length - 1)
        index = np.mod(index, length)
        result = {}
        for pin, levels in self.tracks.items():
            padded = levels if len(levels) == length else np.concatenate(
                [levels, np.full(length - len(levels), levels[-1] if len(levels) else 0, dtype=np.uint8)])
            result[pin] = padded[index]
        return result

    def samples(self, rate: float, count: int) -> np.ndarray | dict[str, np.ndarray]:
        """``count`` samples on ``rate`` from the start (volts, or levels per pin)."""
        times = np.arange(int(count)) / float(rate)
        return self.digital(times) if self.is_pattern else self.analog(times)

    # ------------------------------------------------------------- storage
    def to_data(self) -> dict[str, Any]:
        """The waveform as plain data for ``*.wave.yaml`` (only what differs from the defaults)."""
        defaults = Waveform(kind=self.kind)
        data: dict[str, Any] = {"kind": self.kind}
        names = ["name", "frequency", "amplitude", "offset", "duty", "phase", "rate", "repeat", "modulation",
                 "sweep_start", "sweep_stop", "sweep_time", "burst_cycles", "burst_period", "seed"]
        for name in names:
            value = getattr(self, name)
            if value != getattr(defaults, name):
                data[name] = value
        if self.kind == ARBITRARY:
            if self.formula:
                data["formula"] = self.formula
                data["points_count"] = 0 if self.points is None else len(self.points)
            else:
                if self.source:
                    data["source"] = self.source
                data["points"] = [] if self.points is None else [float(value) for value in self.points]
        if self.is_pattern:
            data["tracks"] = {pin: self.sdl.get(pin) or levels_to_sdl(levels) for pin, levels in self.tracks.items()}
        return data

    @staticmethod
    def from_data(data: dict[str, Any]) -> "Waveform":
        data = dict(data)
        kind = str(data.pop("kind", SINE))
        tracks = data.pop("tracks", None) or {}
        formula = data.pop("formula", "")
        count = int(data.pop("points_count", 0) or DEFAULT_POINTS)
        points = data.pop("points", None)
        quantities = {"frequency": "Hz", "amplitude": "V", "offset": "V", "rate": "Hz", "sweep_start": "Hz",
                      "sweep_stop": "Hz", "sweep_time": "s", "burst_period": "s"}
        values: dict[str, Any] = {}
        for name, value in data.items():
            if name not in Waveform.__dataclass_fields__:
                raise WaveformError(f"unknown waveform setting {name!r}")
            values[name] = float(units.parse(value, quantities[name])) if name in quantities else value
        waveform = Waveform(kind=kind, **values)
        if formula:
            waveform = replace(waveform, points=points_from_formula(formula, count), formula=formula)
        elif points is not None:
            waveform.points = np.asarray(points, dtype=np.float64)
        if tracks:
            pattern = pattern_from_sdl({str(pin): str(text) for pin, text in tracks.items()}, waveform.rate)
            waveform.tracks, waveform.sdl = pattern.tracks, pattern.sdl
        return waveform


DEFAULT_POINTS = 1024


# ----------------------------------------------------------------- builders
def standard(kind: str, frequency: Any = 1000.0, amplitude: Any = 1.0, offset: Any = 0.0, **options) -> Waveform:
    """A standard shape; values may be quantities (``"1 kHz"``, ``"2 V"``)."""
    if kind not in ANALOG_KINDS or kind == ARBITRARY:
        raise WaveformError(f"{kind!r} is no standard shape")
    return Waveform(kind=kind, frequency=float(units.parse(frequency, "Hz")),
                    amplitude=float(units.parse(amplitude, "V")), offset=float(units.parse(offset, "V")), **options)


FORMULA_NAMES = {name: getattr(np, name) for name in (
    "sin", "cos", "tan", "arcsin", "arccos", "arctan", "sinh", "cosh", "tanh", "exp", "log", "log10", "sqrt",
    "abs", "sign", "floor", "ceil", "round", "minimum", "maximum", "clip", "where", "mod", "pi", "e")}


def points_from_formula(formula: str, count: int = DEFAULT_POINTS) -> np.ndarray:
    """One pass of a formula: ``x`` runs from 0 to 1 over the pass (``count`` points), numpy
    functions and ``pi`` are known (``sin(2*pi*x) + 0.3*sin(6*pi*x)``)."""
    if not formula.strip():
        raise WaveformError("an empty formula")
    count = max(int(count), 2)
    x = np.arange(count) / count
    namespace = dict(FORMULA_NAMES, x=x, np=np)
    try:
        values = eval(compile(formula, "<formula>", "eval"), {"__builtins__": {}}, namespace)  # noqa: S307
    except Exception as error:  # noqa: BLE001 - shown to the user
        raise WaveformError(f"formula: {error}") from None
    values = np.broadcast_to(np.asarray(values, dtype=np.float64), x.shape).copy()
    if not np.all(np.isfinite(values)):
        raise WaveformError("the formula is not finite everywhere")
    return values


def from_formula(formula: str, frequency: Any = 1000.0, count: int = DEFAULT_POINTS, **options) -> Waveform:
    return Waveform(kind=ARBITRARY, points=points_from_formula(formula, count), formula=formula,
                    frequency=float(units.parse(frequency, "Hz")), **options)


def from_points(points, rate: Optional[float] = None, frequency: Optional[float] = None, **options) -> Waveform:
    """Arbitrary points (volts); ``rate`` plays them at that sample rate (one pass = len / rate)."""
    points = np.asarray(points, dtype=np.float64)
    if not len(points):
        raise WaveformError("no points")
    if frequency is None:
        frequency = (float(rate) / len(points)) if rate else 1000.0
    return Waveform(kind=ARBITRARY, points=points, frequency=float(frequency), **options)


def from_csv(path: str, frequency: Optional[float] = None) -> Waveform:
    """Points from a CSV file: one column of volts, or time and volts (the times give the rate)."""
    times, values = [], []
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.reader(handle):
            numbers = []
            for cell in row:
                try:
                    numbers.append(float(cell))
                except ValueError:
                    numbers = []
                    break
            if not numbers:
                continue  # a header
            values.append(numbers[-1])
            if len(numbers) > 1:
                times.append(numbers[0])
    if not values:
        raise WaveformError(f"{path}: no numbers")
    rate = None
    if len(times) == len(values) and len(times) > 1:
        step = (times[-1] - times[0]) / (len(times) - 1)
        rate = 1.0 / step if step > 0 else None
    waveform = from_points(values, rate=rate, frequency=frequency)
    waveform.source = path
    return waveform


def from_analog(signal, frequency: Optional[float] = None) -> Waveform:
    """Points from an analog signal (``signals.Analog``) or an ``AnalogChannel`` of a capture."""
    if hasattr(signal, "volts"):
        values, rate = signal.volts(), getattr(signal, "rate", None)
    else:
        values, rate = np.asarray(signal.values, dtype=np.float64), signal.rate
    waveform = from_points(values, rate=rate, frequency=frequency)
    waveform.name = getattr(signal, "channel_name", "") or getattr(signal, "name", "")
    return waveform


def pattern(tracks: dict[str, Any], rate: Any = 1_000_000.0, **options) -> Waveform:
    return Waveform(kind=PATTERN, tracks=dict(tracks), rate=float(units.parse(rate, "Hz")), **options)


def pattern_from_sdl(sources: dict[str, str], rate: Any = 1_000_000.0, **options) -> Waveform:
    """A pattern from SDL text per pin (see :mod:`openscilab.sdl.parser`)."""
    from ..sdl.parser import SDLError, samples_from_source

    tracks = {}
    for pin, text in sources.items():
        try:
            tracks[pin] = samples_from_source(text)
        except SDLError as error:
            raise WaveformError(f"{pin}: {error}") from None
    waveform = pattern(tracks, rate, **options)
    waveform.sdl = dict(sources)
    return waveform


def pattern_from_capture(capture, channels: Optional[list[str]] = None, first: int = 0,
                         last: Optional[int] = None) -> Waveform:
    """A pattern of the digital channels of a capture (``signals.Capture`` or a ``CaptureSession``)."""
    if hasattr(capture, "capture_channels"):
        digital = {channel.display_name if hasattr(channel, "display_name") else str(channel.channel_number):
                   channel.samples for channel in capture.capture_channels if channel.samples is not None}
        rate = float(capture.frequency)
    else:
        digital, rate = dict(capture.digital), float(capture.rate)
    names = channels or list(digital)
    missing = [name for name in names if name not in digital]
    if missing:
        raise WaveformError(f"the capture has no channel {', '.join(missing)}")
    return pattern({name: np.asarray(digital[name][first:last], dtype=np.uint8) for name in names}, rate)


def levels_to_sdl(levels) -> str:
    """Levels as SDL (runs of high and low), the inverse of :func:`pattern_from_sdl`."""
    levels = np.asarray(levels, dtype=np.uint8)
    if not len(levels):
        return ""
    changes = np.flatnonzero(np.diff(levels)) + 1
    starts = np.concatenate([[0], changes])
    ends = np.concatenate([changes, [len(levels)]])
    return "".join(f"{'h' if levels[start] else 'l'}{end - start};" for start, end in zip(starts, ends))


# ------------------------------------------------------------ protocol blocks
def _bits(byte: int, count: int = 8, lsb_first: bool = True) -> list[int]:
    order = range(count) if lsb_first else range(count - 1, -1, -1)
    return [(byte >> bit) & 1 for bit in order]


def _as_bytes(data: Any) -> bytes:
    if isinstance(data, str):
        return data.encode("utf-8")
    return bytes(int(value) & 0xFF for value in data)


def uart_tracks(data: Any, baud: Any = 9600, rate: Any = 1_000_000, pin: str = "TX", idle: int = 2,
                stop_bits: int = 1) -> dict[str, np.ndarray]:
    """8N1 UART frames of ``data`` on ``rate`` (``idle`` bit times of idle high around them)."""
    baud, rate = float(units.parse(baud, "Hz")), float(units.parse(rate, "Hz"))
    per_bit = rate / baud
    if per_bit < 2:
        raise WaveformError("the sample rate must be at least twice the baud rate")
    bits = [1] * idle
    for byte in _as_bytes(data):
        bits += [0] + _bits(byte) + [1] * stop_bits
    bits += [1] * idle
    edges = np.round(np.arange(len(bits) + 1) * per_bit).astype(np.int64)
    return {pin: np.repeat(np.array(bits, dtype=np.uint8), np.diff(edges))}


def spi_tracks(data: Any, clock: Any = 100_000, rate: Any = 1_000_000,
               pins: tuple[str, str, str] = ("CS", "SCK", "MOSI")) -> dict[str, np.ndarray]:
    """SPI mode 0, MSB first: chip select low around the bytes, data valid on the rising edge."""
    clock, rate = float(units.parse(clock, "Hz")), float(units.parse(rate, "Hz"))
    half = int(round(rate / clock / 2))
    if half < 1:
        raise WaveformError("the sample rate must be at least twice the SPI clock")
    cs, sck, mosi = [1] * half * 2, [0] * half * 2, [0] * half * 2
    cs += [0] * half
    sck += [0] * half
    mosi += [0] * half
    for byte in _as_bytes(data):
        for bit in _bits(byte, lsb_first=False):
            cs += [0] * half * 2
            sck += [0] * half + [1] * half
            mosi += [bit] * half * 2
    cs += [0] * half + [1] * half * 2
    sck += [0] * half * 3
    mosi += [0] * half * 3
    return {name: np.array(levels, dtype=np.uint8) for name, levels in zip(pins, (cs, sck, mosi))}


def i2c_tracks(address: int, data: Any, clock: Any = 100_000, rate: Any = 1_000_000, read: bool = False,
               pins: tuple[str, str] = ("SCL", "SDA")) -> dict[str, np.ndarray]:
    """An I²C write (or read request) to ``address`` with ``data``; the acknowledges are driven low
    (as if the target answered)."""
    clock, rate = float(units.parse(clock, "Hz")), float(units.parse(rate, "Hz"))
    quarter = int(round(rate / clock / 4))
    if quarter < 1:
        raise WaveformError("the sample rate must be at least four times the I²C clock")
    scl, sda = [1] * quarter * 4, [1] * quarter * 4
    # start: SDA falls while SCL is high
    scl += [1] * quarter * 2
    sda += [1] * quarter + [0] * quarter
    scl += [0] * quarter
    sda += [0] * quarter

    def bit(value: int) -> None:
        sda.extend([value] * quarter * 4)
        scl.extend([0] * quarter + [1] * quarter * 2 + [0] * quarter)

    for byte in [(int(address) << 1) | int(read)] + list(_as_bytes(data)):
        for value in _bits(byte, lsb_first=False):
            bit(value)
        bit(0)  # acknowledge
    # stop: SDA rises while SCL is high
    sda += [0] * quarter * 2 + [1] * quarter * 4
    scl += [0] * quarter + [1] * quarter * 5
    return {pins[0]: np.array(scl, dtype=np.uint8), pins[1]: np.array(sda, dtype=np.uint8)}


def combine(*parts: dict[str, np.ndarray], idle: Optional[dict[str, int]] = None) -> dict[str, np.ndarray]:
    """Tracks of several blocks one after the other (pins missing in a block hold their level)."""
    pins: list[str] = []
    for part in parts:
        pins += [pin for pin in part if pin not in pins]
    levels = {pin: (idle or {}).get(pin, 0) for pin in pins}
    result: dict[str, list[np.ndarray]] = {pin: [] for pin in pins}
    for part in parts:
        length = max((len(values) for values in part.values()), default=0)
        for pin in pins:
            values = part.get(pin)
            if values is None or not len(values):
                values = np.full(length, levels[pin], dtype=np.uint8)
            elif len(values) < length:
                values = np.concatenate([values, np.full(length - len(values), values[-1], dtype=np.uint8)])
            result[pin].append(np.asarray(values, dtype=np.uint8))
            levels[pin] = int(values[-1]) if len(values) else levels[pin]
    return {pin: np.concatenate(chunks) if chunks else np.zeros(0, np.uint8) for pin, chunks in result.items()}


# ------------------------------------------------------------------- limits
def problems(waveform: Waveform, output) -> list[str]:
    """What keeps ``waveform`` from playing on ``output`` (an ``OutputInfo``); empty when it fits."""
    found = []
    if output.kind == "pattern":
        if not waveform.is_pattern:
            found.append(f"{output.name} plays digital patterns, not {waveform.kind} waveforms")
            return found
        if waveform.rate > output.max_rate:
            found.append(f"the pattern rate {units.format_quantity(waveform.rate, 'Hz')} is above the "
                         f"{units.format_quantity(output.max_rate, 'Hz')} of {output.name}")
        if waveform.pattern_length > output.max_points:
            found.append(f"{waveform.pattern_length} samples are more than the {output.max_points} "
                         f"{output.name} holds")
        if output.pins:
            unknown = [pin for pin in waveform.tracks if pin not in output.pins]
            if unknown:
                found.append(f"{output.name} has no pin {', '.join(unknown)} (pins: {', '.join(output.pins)})")
        return found
    if waveform.is_pattern:
        found.append(f"{output.name} is an analog output, not a pattern generator")
        return found
    if output.kind == "square" and waveform.kind not in (SQUARE, PULSE):
        found.append(f"{output.name} makes square waves only (PWM)")
    if waveform.highest_frequency() > output.max_frequency:
        found.append(f"{units.format_quantity(waveform.highest_frequency(), 'Hz')} is above the "
                     f"{units.format_quantity(output.max_frequency, 'Hz')} of {output.name}")
    low, high = waveform.voltage_range()
    allowed_low, allowed_high = output.voltage_range
    if low < allowed_low - 1e-9 or high > allowed_high + 1e-9:
        found.append(f"{units.format_quantity(low, 'V')} to {units.format_quantity(high, 'V')} is outside "
                     f"the {units.format_quantity(allowed_low, 'V')} to "
                     f"{units.format_quantity(allowed_high, 'V')} of {output.name}")
    if waveform.kind == ARBITRARY and waveform.points is not None and len(waveform.points) > output.max_points:
        found.append(f"{len(waveform.points)} points are more than the {output.max_points} of {output.name}")
    if waveform.modulation and waveform.modulation.upper() not in output.capabilities:
        found.append(f"{output.name} cannot {waveform.modulation}")
    return found


def fit_points(waveform: Waveform, max_points: int) -> Waveform:
    """An arbitrary waveform with at most ``max_points`` points (evenly resampled)."""
    if waveform.kind != ARBITRARY or waveform.points is None or len(waveform.points) <= max_points:
        return waveform
    index = (np.arange(max_points) * len(waveform.points) / max_points).astype(np.int64)
    return replace(waveform, points=waveform.points[index])


def limit_rate(waveform: Waveform, max_rate: float) -> Waveform:
    """A pattern faster than ``max_rate``: every n-th sample at ``rate / n`` (same duration); edges
    closer than n samples get lost."""
    if not waveform.is_pattern or waveform.rate <= max_rate or max_rate <= 0:
        return waveform
    factor = int(math.ceil(waveform.rate / max_rate))
    return replace(waveform, rate=waveform.rate / factor, sdl={},
                   tracks={pin: levels[::factor] for pin, levels in waveform.tracks.items()})


# --------------------------------------------------------------------- files
WAVE_SUFFIX = ".wave.yaml"


def save(waveform: Waveform, path: str) -> None:
    from .files import atomic_open

    with atomic_open(path) as handle:
        yaml_text.dump(waveform.to_data(), handle)


def load(path: str, pin: str = "D0", rate: Any = 1_000_000.0) -> Waveform:
    """A ``*.wave.yaml`` file, or an ``*.sdl`` file (one track: on ``pin`` at ``rate``)."""
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    if path.lower().endswith(".sdl"):
        return pattern_from_sdl({pin: text}, rate)
    import yaml

    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise WaveformError(f"{path}: not a waveform")
    return Waveform.from_data(data)
