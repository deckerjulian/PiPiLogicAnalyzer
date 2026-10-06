# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The virtual circuit around a simulated instrument: what its inputs see.

A circuit has named *nets* (``D3``, ``A0``, ``CH1``); each is driven by a *source*: a function
of time (square, clock, counter, UART, ...). The device model (:mod:`.device`) samples the nets
of its inputs with its own rate, depth and limits; the circuit knows nothing about devices.

Times are seconds of the instrument's clock. Every source is a pure function of time (and a
seed), so a capture of the same interval gives the same samples – the fast mode is
deterministic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import numpy as np

from ...core import simulation, units

#: added before rounding down, so a sample exactly on an edge is not moved by float errors
_EPSILON = 1e-7


def _phase(start: float, rate: float, count: int, frequency: float) -> np.ndarray:
    """``(start + i / rate) * frequency`` for ``i < count``, without accumulating float errors."""
    return start * frequency + np.arange(count, dtype=np.float64) * (frequency / rate)


class Source:
    """Drives a net. ``digital()`` gives logic levels, ``analog()`` volts."""

    kind = "source"
    #: volts of a logic 1 (for analog sampling of a digital source)
    high = 3.3
    #: the fastest change of the source in Hz (a block longer than half a period of it holds both
    #: extremes); ``None``: unknown, the envelope is sampled
    frequency: Optional[float] = None

    def digital(self, start: float, rate: float, count: int) -> np.ndarray:
        return (self.analog(start, rate, count) >= self.high / 2).astype(np.uint8)

    def analog(self, start: float, rate: float, count: int) -> np.ndarray:
        return self.digital(start, rate, count).astype(np.float64) * self.high

    def value_range(self, analog: bool) -> tuple[float, float]:
        """Lowest and highest value the source reaches."""
        return (0.0, 1.0) if not analog else (0.0, self.high)

    def envelope(self, start: float, rate: float, count: int, block: int, analog: bool = False
                 ) -> tuple[np.ndarray, np.ndarray]:
        """Minimum and maximum of every ``block`` samples of ``count`` (the overview of a large
        capture, without computing every sample): blocks longer than half the period of the
        source's fastest change hold its whole range, shorter ones are sampled 8 times."""
        blocks = (count + block - 1) // block
        low, high = self.value_range(analog)
        duration = block / rate
        if self.frequency is not None and duration * self.frequency >= 0.5:
            return np.full(blocks, low), np.full(blocks, high)
        points = 8
        times = start + (np.arange(blocks * points) * (block / points)) / rate
        step = (times[1] - times[0]) if len(times) > 1 else 1.0
        values = np.empty(len(times))
        # sampled one point at a time through the source's own function (rate = 1 / step)
        sampled = self.analog(times[0], 1.0 / step, len(times)) if analog else \
            self.digital(times[0], 1.0 / step, len(times)).astype(np.float64)
        values[:] = sampled
        values = values.reshape(blocks, points)
        return values.min(axis=1), values.max(axis=1)

    def describe(self) -> str:
        return self.kind


@dataclass
class Constant(Source):
    """A fixed level: 0 and 1 are logic levels, other values volts (1 above half the logic level)."""

    level: float = 0.0
    kind = "constant"

    def digital(self, start, rate, count):
        high = self.level == 1 if self.level in (0, 1) else self.level >= self.high / 2
        return np.full(count, 1 if high else 0, dtype=np.uint8)

    def analog(self, start, rate, count):
        return np.full(count, float(self.level), dtype=np.float64)

    def describe(self) -> str:
        if self.level == 0:
            return "low (nothing connected)"
        return "high" if self.level == 1 else f"{self.level:g} V"


@dataclass
class Square(Source):
    frequency: float = 1000.0
    duty: float = 0.5
    phase: float = 0.0
    kind = "square"

    def analog(self, start, rate, count):
        return self.digital(start, rate, count).astype(np.float64) * self.high

    def digital(self, start, rate, count):
        cycle = _phase(start, rate, count, self.frequency) + self.phase
        return ((cycle - np.floor(cycle + _EPSILON)) < self.duty - _EPSILON).astype(np.uint8)

    def describe(self) -> str:
        return f"square {units.format_quantity(self.frequency, 'Hz')}, {self.duty * 100:g} %"


@dataclass
class Counter(Source):
    """Bit ``bit`` of a counter that counts with ``frequency`` (on the rising edges of a clock of
    the same frequency)."""

    frequency: float = 1000.0
    bit: int = 0
    kind = "counter"

    def digital(self, start, rate, count):
        value = np.floor(_phase(start, rate, count, self.frequency) + _EPSILON).astype(np.int64)
        return ((value >> self.bit) & 1).astype(np.uint8)

    def envelope(self, start, rate, count, block, analog=False):
        # bit n changes every 2**n counts; on a copy, so a capture in another thread keeps its rate
        slowed = Counter(frequency=self.frequency / (2 ** self.bit), bit=0)
        slowed.digital = self.digital  # type: ignore[method-assign]
        return Source.envelope(slowed, start, rate, count, block, analog)

    def describe(self) -> str:
        return f"counter bit {self.bit} at {units.format_quantity(self.frequency, 'Hz')}"


@dataclass
class Uart(Source):
    """8N1 frames of ``text``, then ``gap`` bits idle, repeated."""

    text: str = "openSciLab\n"
    baud: float = 115200.0
    gap: int = 20
    kind = "uart"

    @property
    def frequency(self) -> float:  # type: ignore[override]
        return self.baud

    def digital(self, start, rate, count):
        data = np.frombuffer(self.text.encode("utf-8") or b"\0", dtype=np.uint8).astype(np.int64)
        period = self.gap + 10 * len(data)
        bit = np.floor(_phase(start, rate, count, self.baud) + _EPSILON).astype(np.int64) % period
        position = bit - self.gap
        in_frame = position >= 0
        frame_bit = np.where(in_frame, position % 10, 9)
        byte = data[np.clip(position // 10, 0, len(data) - 1)]
        level = np.where(frame_bit == 0, 0, np.where(frame_bit == 9, 1, (byte >> np.clip(frame_bit - 1, 0, 7)) & 1))
        return np.where(in_frame, level, 1).astype(np.uint8)

    def describe(self) -> str:
        return f"UART {self.baud:g} Bd: {self.text!r}"


@dataclass
class Protocol(Source):
    """A line of the SPI or I²C test signals of the firmware (``core/simulation.py``), clocked with
    ``frequency`` steps per second."""

    line: str = "spi.clk"
    frequency: float = 1e6
    kind = "protocol"

    def digital(self, start, rate, count):
        index = np.floor(_phase(start, rate, count, self.frequency) + _EPSILON).astype(np.int64)
        name = self.line.lower()
        if name.startswith("spi"):
            clk, mosi, cs = simulation._spi(index)
            values = {"spi.clk": clk, "spi.mosi": mosi, "spi.cs": cs}[name]
        elif name.startswith("i2c"):
            scl, sda = simulation._i2c(index)
            values = {"i2c.scl": scl, "i2c.sda": sda}[name]
        else:
            raise ValueError(f"unknown protocol line {self.line!r}")
        return np.asarray(values, dtype=np.uint8)

    def describe(self) -> str:
        return self.line.upper()


def _hash_noise(step: np.ndarray, seed: int) -> np.ndarray:
    """Deterministic pseudo-random 64-bit values per step."""
    mixed = (step.astype(np.uint64) * np.uint64(0x9E3779B97F4A7C15) + np.uint64(seed)) & np.uint64(0xFFFFFFFFFFFFFFFF)
    mixed ^= mixed >> np.uint64(31)
    mixed = (mixed * np.uint64(0xBF58476D1CE4E5B9)) & np.uint64(0xFFFFFFFFFFFFFFFF)
    mixed ^= mixed >> np.uint64(27)
    return mixed


@dataclass
class Noise(Source):
    """Random levels changing ``frequency`` times per second (deterministic per seed)."""

    frequency: float = 1000.0
    seed: int = 1
    kind = "noise"

    def digital(self, start, rate, count):
        step = np.floor(_phase(start, rate, count, self.frequency) + _EPSILON).astype(np.int64)
        return (_hash_noise(step, self.seed) & np.uint64(1)).astype(np.uint8)


# ------------------------------------------------------------------- analog
@dataclass
class Sine(Source):
    frequency: float = 1000.0
    amplitude: float = 1.0
    offset: float = 0.0
    phase: float = 0.0
    kind = "sine"

    def analog(self, start, rate, count):
        cycle = _phase(start, rate, count, self.frequency) + self.phase
        return self.offset + self.amplitude * np.sin(2 * np.pi * cycle)

    def digital(self, start, rate, count):
        return (self.analog(start, rate, count) >= self.offset).astype(np.uint8)

    def value_range(self, analog):
        return (self.offset - abs(self.amplitude), self.offset + abs(self.amplitude)) if analog else (0.0, 1.0)

    def describe(self) -> str:
        return f"sine {units.format_quantity(self.frequency, 'Hz')}, {self.amplitude:g} V + {self.offset:g} V"


@dataclass
class Triangle(Source):
    frequency: float = 1000.0
    amplitude: float = 1.0
    offset: float = 0.0
    phase: float = 0.0
    kind = "triangle"

    def analog(self, start, rate, count):
        cycle = (_phase(start, rate, count, self.frequency) + self.phase) % 1.0
        return self.offset + self.amplitude * (4 * np.abs(cycle - 0.5) - 1)

    def digital(self, start, rate, count):
        return (self.analog(start, rate, count) >= self.offset).astype(np.uint8)

    def value_range(self, analog):
        return (self.offset - abs(self.amplitude), self.offset + abs(self.amplitude)) if analog else (0.0, 1.0)


@dataclass
class Ramp(Source):
    """A sawtooth from ``low`` to ``high`` every period."""

    frequency: float = 100.0
    low: float = 0.0
    high_level: float = 3.3
    kind = "ramp"

    def analog(self, start, rate, count):
        cycle = _phase(start, rate, count, self.frequency) % 1.0
        return self.low + (self.high_level - self.low) * cycle

    def digital(self, start, rate, count):
        return (self.analog(start, rate, count) >= (self.low + self.high_level) / 2).astype(np.uint8)

    def value_range(self, analog):
        return (self.low, self.high_level) if analog else (0.0, 1.0)


@dataclass
class AnalogNoise(Source):
    """Gaussian-like noise of ``rms`` volts around ``offset``, ``bandwidth`` new values per second."""

    rms: float = 0.1
    offset: float = 0.0
    bandwidth: float = 1e6
    seed: int = 1
    kind = "analog_noise"

    @property
    def frequency(self) -> float:  # type: ignore[override]
        return self.bandwidth

    def analog(self, start, rate, count):
        step = np.floor(_phase(start, rate, count, self.bandwidth) + _EPSILON).astype(np.int64)
        # sum of four uniform values: close enough to a normal distribution, deterministic
        total = np.zeros(count)
        for part in range(4):
            total += (_hash_noise(step * 4 + part, self.seed) >> np.uint64(11)).astype(np.float64) / 2.0**53
        return self.offset + (total - 2.0) * self.rms * np.sqrt(3.0)

    def value_range(self, analog):
        return (self.offset - 3.5 * self.rms, self.offset + 3.5 * self.rms) if analog else (0.0, 1.0)


@dataclass
class Sum(Source):
    """Several sources added (a sine with noise)."""

    parts: tuple = ()
    kind = "sum"

    @property
    def frequency(self) -> Optional[float]:  # type: ignore[override]
        frequencies = [part.frequency for part in self.parts if part.frequency]
        return max(frequencies) if frequencies else None

    def analog(self, start, rate, count):
        total = np.zeros(count)
        for part in self.parts:
            total += part.analog(start, rate, count)
        return total

    def value_range(self, analog):
        if not analog:
            return 0.0, 1.0
        ranges = [part.value_range(True) for part in self.parts]
        return sum(low for low, _high in ranges), sum(high for _low, high in ranges)

    def envelope(self, start, rate, count, block, analog=False):
        if not analog:
            return Source.envelope(self, start, rate, count, block, analog)
        # The extremes of a sum lie within the sums of the parts' extremes.
        low = high = None
        for part in self.parts:
            part_low, part_high = part.envelope(start, rate, count, block, True)
            low = part_low if low is None else low + part_low
            high = part_high if high is None else high + part_high
        blocks = (count + block - 1) // block
        return (low if low is not None else np.zeros(blocks)), (high if high is not None else np.zeros(blocks))


# --------------------------------------------------- outputs of the device
class OutputSource(Source):
    """A pin the device drives: levels from given times on (``None``: not driven, high impedance)."""

    kind = "output"

    def __init__(self, high: float = 3.3) -> None:
        self.high = high
        #: (time, level) with level 0/1 or None (released), in time order
        self.changes: list[tuple[float, Optional[int]]] = []
        #: PWM from a time on: (time, frequency, duty)
        self.pwm: list[tuple[float, float, float]] = []
        #: what the net reads when not driven (pull-up: 1)
        self.idle = 0
        #: the signal connected to the pin from outside: what the net carries while the output
        #: does not drive it (``None``: nothing, the net reads ``idle``)
        self.below: Optional[Source] = None
        self.frequency = None

    # ``changes`` and ``pwm`` are replaced, never changed in place: a capture running in another
    # thread reads one list from start to end.
    def set(self, time: float, level: Optional[int]) -> None:
        self.pwm = [segment for segment in self.pwm if segment[0] < time]
        self.changes = [change for change in self.changes if change[0] < time] + [(time, level)]

    def add(self, time: float, level: Optional[int]) -> None:
        """A further change after the existing ones (the end of a pulse)."""
        self.changes = sorted(self.changes + [(time, level)], key=lambda change: change[0])

    def set_pwm(self, time: float, frequency: float, duty: float) -> None:
        self.pwm = [segment for segment in self.pwm if segment[0] < time] + [(time, float(frequency), float(duty))]
        # 2: PWM drives the pin
        self.changes = [change for change in self.changes if change[0] < time] + [(time, None if duty <= 0 else 2)]

    def level_at(self, time: float) -> Optional[int]:
        level: Optional[int] = None
        for when, value in self.changes:
            if when <= time + 1e-12:
                level = value
            else:
                break
        return level

    def digital(self, start, rate, count):
        changes, pwm, below = self.changes, self.pwm, self.below  # one snapshot (see ``set``)
        times = start + np.arange(count) / rate
        if below is not None and not isinstance(below, OutputSource):
            result = np.asarray(below.digital(start, rate, count), dtype=np.uint8)
        else:
            result = np.full(count, self.idle, dtype=np.uint8)
        if not changes:
            return result
        stamps = np.array([when for when, _value in changes])
        index = np.searchsorted(stamps, times + 1e-12, side="right") - 1
        levels = np.array([-1 if value is None else value for _when, value in changes])
        chosen = np.where(index >= 0, levels[np.clip(index, 0, None)], -1)
        result = np.where(chosen == 1, 1, np.where(chosen == 0, 0, result)).astype(np.uint8)
        pwm_mask = chosen == 2
        if pwm_mask.any() and pwm:
            pwm_stamps = np.array([segment[0] for segment in pwm])
            segment = np.clip(np.searchsorted(pwm_stamps, times + 1e-12, side="right") - 1, 0, len(pwm) - 1)
            frequency = np.array([item[1] for item in pwm])[segment]
            duty = np.array([item[2] for item in pwm])[segment]
            begin = np.array([item[0] for item in pwm])[segment]
            phase = ((times - begin) * frequency) % 1.0
            result = np.where(pwm_mask, (phase < duty - _EPSILON).astype(np.uint8), result)
        return result.astype(np.uint8)

    def average(self, time: float) -> float:
        """The mean level (volts) at ``time``: the duty cycle times the high level for PWM."""
        level = self.level_at(time)
        if level == 2 and self.pwm:
            segment = max((item for item in self.pwm if item[0] <= time + 1e-12), key=lambda item: item[0],
                          default=self.pwm[-1])
            return segment[2] * self.high
        if level is None:
            return self.idle * self.high
        return float(level) * self.high

    def change_times(self) -> list[float]:
        return [when for when, _value in self.changes]

    def describe(self) -> str:
        changes = self.changes
        level = changes[-1][1] if changes else None
        if level is None and self.below is not None:
            return self.below.describe()  # an input again: what is connected to it
        return {None: "not driven", 0: "output low", 1: "output high", 2: "PWM"}.get(level, "output")


class Alias(Source):
    """A wire: the net shows what another net carries (``threshold``: an analog net read as logic)."""

    kind = "wire"

    def __init__(self, circuit: "Circuit", net: str, threshold: Optional[float] = None) -> None:
        self.circuit = circuit
        self.net = net
        self.threshold = threshold

    @property
    def frequency(self):  # type: ignore[override]
        source = self.circuit.sources.get(self.net)
        return source.frequency if source is not None else None

    def digital(self, start, rate, count):
        if self.threshold is not None:
            return (self.circuit.analog(self.net, start, rate, count) >= self.threshold).astype(np.uint8)
        return self.circuit.digital(self.net, start, rate, count)

    def analog(self, start, rate, count):
        return self.circuit.analog(self.net, start, rate, count)

    def value_range(self, analog):
        source = self.circuit.sources.get(self.net)
        return source.value_range(analog) if source is not None else (0.0, 1.0)

    def describe(self) -> str:
        return f"wired to {self.net}" + (f" above {self.threshold:g} V" if self.threshold is not None else "")


def _low_pass(values: np.ndarray, alpha: float) -> np.ndarray:
    """``y[n] = y[n-1] + alpha * (x[n] - y[n-1])`` from ``y[-1] = x[0]``, without a loop over the samples:
    in blocks short enough that the powers of ``1 - alpha`` stay finite."""
    result = np.empty(len(values))
    decay = 1.0 - alpha
    block = max(int(500 / max(-math.log(decay), 1e-12)), 1) if decay > 0 else 1
    previous = float(values[0]) if len(values) else 0.0
    for begin in range(0, len(values), block):
        part = values[begin:begin + block]
        powers = decay ** np.arange(1, len(part) + 1)
        result[begin:begin + len(part)] = powers * (previous + alpha * np.cumsum(part / powers))
        previous = float(result[begin + len(part) - 1])
    return result


class RCFilter(Source):
    """An RC low pass after a net (PWM → voltage): first order. After a device output from the mean of
    its levels (the ripple of a PWM much faster than ``r * c`` is left out); after any other source
    (a generator, a DAC) from its voltage, sampled finely from five time constants before."""

    kind = "rc"

    def __init__(self, circuit: "Circuit", net: str, r: float, c: float) -> None:
        self.circuit = circuit
        self.net = net
        self.tau = float(r) * float(c)
        self.frequency = None

    def _source(self) -> Optional[Source]:
        source = self.circuit.sources.get(self.net)
        while isinstance(source, Alias):
            source = self.circuit.sources.get(source.net)
        return source

    def _input(self) -> Optional[OutputSource]:
        source = self._source()
        return source if isinstance(source, OutputSource) else None

    @property
    def high(self) -> float:  # (the logic level of what drives it: its digital reading switches at half)
        source = self._source()
        return float(getattr(source, "high", 3.3)) if source is not None else 3.3

    def _filtered(self, source: Source, start: float, rate: float, count: int) -> np.ndarray:
        if self.tau <= 0:
            return np.asarray(source.analog(start, rate, count), dtype=np.float64)
        warm = 5 * self.tau
        length = warm + count / rate
        fine = min(max(rate, 20.0 / self.tau, 20.0 * float(source.frequency or 0.0)), 4e6 / length)
        total = int(math.ceil(length * fine)) + 1
        values = np.asarray(source.analog(start - warm, fine, total), dtype=np.float64)
        filtered = _low_pass(values, 1.0 - math.exp(-1.0 / (fine * self.tau)))
        times = warm + np.arange(count) / rate
        return np.interp(times, np.arange(total) / fine, filtered)

    def analog(self, start, rate, count):
        times = start + np.arange(count) / rate
        source = self._input()
        if source is None:
            other = self._source()
            return np.zeros(count) if other is None else self._filtered(other, start, rate, count)
        if not source.changes:
            return np.zeros(count)
        # The voltage after each change of the input: v -> target with time constant tau.
        result = np.zeros(count)
        voltage = 0.0
        moments = sorted(set(source.change_times() + [item[0] for item in source.pwm]))
        previous = moments[0]
        segments = []
        for moment in moments:
            target_before = source.average(previous)
            voltage = target_before + (voltage - target_before) * np.exp(-(moment - previous) / self.tau)
            segments.append((moment, voltage, source.average(moment)))
            previous = moment
        stamps = np.array([segment[0] for segment in segments])
        index = np.searchsorted(stamps, times + 1e-12, side="right") - 1
        before = index < 0
        index = np.clip(index, 0, len(segments) - 1)
        begin = stamps[index]
        start_voltage = np.array([segment[1] for segment in segments])[index]
        target = np.array([segment[2] for segment in segments])[index]
        result = target + (start_voltage - target) * np.exp(-(times - begin) / self.tau)
        return np.where(before, 0.0, result)

    def value_range(self, analog):
        source = self._source()
        return (source.value_range(True) if source is not None else (0.0, 5.0)) if analog else (0.0, 1.0)

    def describe(self) -> str:
        return f"RC low pass of {self.net} (τ = {units.format_quantity(self.tau, 's', 3)})"


@dataclass
class Button(Source):
    """A push button: pressed (1, or 0 with ``active_low``) in the ``presses`` (start, end) seconds,
    bouncing ``bounces`` times within ``bounce`` seconds after each change."""

    presses: tuple = ()
    bounce: float = 0.002
    bounces: int = 4
    active_low: bool = True
    seed: int = 1
    kind = "button"

    def __post_init__(self) -> None:
        self.presses = tuple(tuple(float(units.parse(value, "s")) for value in press) for press in self.presses)
        self.bounce = float(units.parse(self.bounce, "s"))
        self.frequency = None

    def press(self, start: float, duration: float) -> None:
        self.presses = tuple(sorted(self.presses + ((start, start + duration),)))

    def digital(self, start, rate, count):
        times = start + np.arange(count) / rate
        pressed = np.zeros(count, dtype=bool)
        for down, up in self.presses:
            pressed |= (times >= down) & (times < up)
            for index, edge in enumerate((down, up)):
                # Bounces: short pulses of the other level right after the edge.
                for bounce in range(self.bounces):
                    width = self.bounce / (2 * self.bounces)
                    at = edge + (bounce * 2 + 1) * width
                    glitch = (times >= at) & (times < at + width * 0.6)
                    pressed = np.where(glitch, index == 1, pressed)
        level = pressed if not self.active_low else ~pressed
        return level.astype(np.uint8)


#: how long a stopped generator stays in the circuit's past (captures look back that far at most)
KEEP_PAST = 60.0


class WaveSource(Source):
    """A generator output: a waveform (``core.waveform``) from ``start`` until ``end`` (when it was
    stopped), ``before`` outside that time.

    ``pin`` picks the track of a pattern."""

    kind = "generator"

    def __init__(self, waveform, start: float, pin: Optional[str] = None, high: float = 3.3,
                 before: Optional[Source] = None) -> None:
        self.waveform = waveform
        self.start = float(start)
        #: when the output was stopped (``None``: it plays)
        self.end: Optional[float] = None
        self.pin = pin
        self.high = high
        self.before = before
        self.frequency = waveform.rate / 2 if waveform.is_pattern else waveform.highest_frequency() or None

    def _own(self, start, rate, count, analog: bool) -> np.ndarray:
        times = start + np.arange(count) / rate - self.start
        if self.waveform.is_pattern:
            levels = self.waveform.digital(times).get(self.pin, np.zeros(count, dtype=np.uint8))
            return levels.astype(np.float64) * self.high if analog else levels
        volts = self.waveform.analog(times)
        return volts if analog else (volts >= self.high / 2).astype(np.uint8)

    def _blend(self, start, rate, count, analog: bool) -> np.ndarray:
        own = self._own(start, rate, count, analog)
        first = min(max(int(math.ceil((self.start - start) * rate - 1e-9)), 0), count)
        last = count if self.end is None else min(max(int(math.ceil((self.end - start) * rate - 1e-9)), first), count)
        for begin, stop in ((0, first), (last, count)):
            if stop <= begin:
                continue
            if self.before is None:
                own[begin:stop] = 0
            else:
                at = start + begin / rate
                own[begin:stop] = self.before.analog(at, rate, stop - begin) if analog else \
                    self.before.digital(at, rate, stop - begin)
        return own

    def ended_before(self, time: float) -> bool:
        return self.end is not None and self.end < time

    def digital(self, start, rate, count):
        return self._blend(start, rate, count, False).astype(np.uint8)

    def analog(self, start, rate, count):
        return self._blend(start, rate, count, True).astype(np.float64)

    def value_range(self, analog):
        if self.waveform.is_pattern:
            return (0.0, self.high) if analog else (0.0, 1.0)
        low, high = self.waveform.voltage_range()
        if self.before is not None:
            other = self.before.value_range(analog)
            low, high = min(low, other[0]), max(high, other[1])
        return (low, high) if analog else (0.0, 1.0)


class SyncSource(Source):
    """The sync output of a generator: high for the first half of every period from ``start`` on."""

    kind = "sync"

    def __init__(self, start: float, period: float, high: float = 3.3) -> None:
        self.start = float(start)
        #: when the generator was stopped (``None``: it plays)
        self.end: Optional[float] = None
        self.period = float(period)
        self.high = high
        self.frequency = 1.0 / period if period > 0 else None

    def digital(self, start, rate, count):
        times = start + np.arange(count) / rate - self.start
        playing = times >= 0
        if self.end is not None:
            playing &= times < self.end - self.start
        if self.period <= 0 or not math.isfinite(self.period):
            return playing.astype(np.uint8)
        phase = np.mod(np.maximum(times, 0), self.period) / self.period
        return (playing & (phase < 0.5)).astype(np.uint8)

    def ended_before(self, time: float) -> bool:
        return self.end is not None and self.end < time


class RemoteSource(Source):
    """A net of another simulated device, wired over the hub (a trigger route): its time is this
    device's time plus ``offset`` (the difference of the two clocks) minus the cable ``delay``."""

    kind = "remote"

    def __init__(self, circuit: "Circuit", net: str, offset: float, delay: float = 0.0, high: float = 3.3) -> None:
        self.circuit = circuit
        self.net = net
        self.offset = float(offset)
        self.delay = float(delay)
        self.high = high
        self.frequency = None

    def digital(self, start, rate, count):
        return self.circuit.digital(self.net, start + self.offset - self.delay, rate, count)

    def analog(self, start, rate, count):
        return self.circuit.analog(self.net, start + self.offset - self.delay, rate, count)

    def describe(self) -> str:
        return f"wired from {self.net}"


class Replay(Source):
    """A recording played back: its samples at ``frequency`` samples per second, repeated."""

    kind = "replay"

    def __init__(self, samples: np.ndarray, frequency: float, label: str = "recording", high: float = 3.3) -> None:
        self.samples = np.asarray(samples, dtype=np.uint8)
        if self.samples.size == 0:
            self.samples = np.zeros(1, dtype=np.uint8)
        self.rate = float(frequency)
        self.label = label
        self.high = high
        #: a line that never changes has no fastest change (its overview is not "both levels")
        self.frequency = None if int(self.samples.min()) == int(self.samples.max()) else self.rate / 2

    def digital(self, start, rate, count):
        index = np.floor(_phase(start, rate, count, self.rate) + _EPSILON).astype(np.int64) % len(self.samples)
        return self.samples[index]

    def value_range(self, analog):
        low, high = float(self.samples.min()), float(self.samples.max())
        return (low * self.high, high * self.high) if analog else (low, high)

    def describe(self) -> str:
        return self.label


def _c64(line: str = "Φ2", **options) -> Replay:
    """A line of the C64 expansion port (``core/c64_model.py``): ``A0``…``A15``, ``D0``…``D7``,
    ``Φ2``, ``R/W``, ``/RESET``, ``/IRQ``, …"""
    from ...core import c64_model

    lines = c64_model.signals()
    if line not in lines:
        raise ValueError(f"the C64 bus has no line {line!r} (known: {', '.join(lines)})")
    return Replay(lines[line], c64_model.SAMPLE_RATE, f"C64 {line}", **options)


_capture_cache: dict[tuple[str, float], Any] = {}


def _capture_file(path: str):
    """The capture in ``path`` (kept while the file does not change)."""
    import os

    path = os.path.abspath(os.path.expanduser(path))
    key = (path, os.path.getmtime(path))
    if key not in _capture_cache:
        from ...core import capture_io

        _capture_cache.clear()  # one file at a time is enough; captures can be large
        _capture_cache[key] = capture_io.load_capture(path).session
    return _capture_cache[key]


def _file(path: str = "", channel: int = 0, **options) -> Replay:
    """Channel number ``channel`` of the capture file ``path``, at the rate it was captured with."""
    if not path:
        raise ValueError("a file source needs the path of a capture")
    try:
        session = _capture_file(path)
    except (OSError, ValueError, KeyError) as error:
        raise ValueError(f"{path} cannot be read: {error}") from None
    found = next((item for item in session.capture_channels if item.channel_number == int(channel)), None)
    if found is None or found.samples is None:
        raise ValueError(f"{path} has no channel {channel}")
    return Replay(found.samples, session.frequency, f"{found.display_name} of {path.rsplit('/', 1)[-1]}", **options)


#: Source types of profile files: ``{"type": "square", "frequency": "1 kHz"}``.
SOURCE_TYPES: dict[str, Callable[..., Source]] = {
    "constant": Constant,
    "square": Square,
    "clock": Square,
    "counter": Counter,
    "uart": Uart,
    "protocol": Protocol,
    "spi": lambda line="clk", **options: Protocol(line=f"spi.{line}", **options),
    "i2c": lambda line="scl", **options: Protocol(line=f"i2c.{line}", **options),
    "noise": Noise,
    "sine": Sine,
    "triangle": Triangle,
    "ramp": Ramp,
    "analog_noise": AnalogNoise,
    "button": Button,
    "c64": _c64,
    "file": _file,
}


def _pulse(period: Any = "1 ms", width: Any = "100 us", **options) -> Square:
    """A pulse train: ``width`` high every ``period``."""
    period, width = float(units.parse(period, "s")), float(units.parse(width, "s"))
    if period <= 0 or not 0 < width < period:
        raise ValueError("a pulse train needs 0 < width < period")
    return Square(frequency=1.0 / period, duty=width / period, **options)


SOURCE_TYPES["pulse"] = _pulse

#: Parameters given as quantities with a unit
_QUANTITY_PARAMS = {"frequency": "Hz", "baud": "", "duty": "", "phase": "", "level": "", "amplitude": "V",
                    "offset": "V", "low": "V", "high_level": "V", "rms": "V", "bandwidth": "Hz"}


def make_source(description: dict, high: Optional[float] = None) -> Source:
    """The source of ``description``; ``high``: the volts of a logic 1 on the device (its logic level),
    unless the description gives them."""
    options = dict(description)
    kind = options.pop("type", "constant")
    if kind == "sum":
        return Sum(parts=tuple(make_source(part, high) for part in options.get("parts", [])))
    factory = SOURCE_TYPES.get(kind)
    if factory is None:
        raise ValueError(f"unknown source type {kind!r} (known: {', '.join(sorted(SOURCE_TYPES))})")
    for key, unit in _QUANTITY_PARAMS.items():
        if key in options and isinstance(options[key], str):
            options[key] = units.parse(options[key], unit)
    if "rate" in options and "frequency" not in options:
        options["frequency"] = units.parse(options.pop("rate"), "Hz")
    source = factory(**options)
    if high is not None and "high" not in description:
        source.high = float(high)
    return source


@dataclass
class Circuit:
    """Nets and the sources driving them."""

    sources: dict[str, Source] = field(default_factory=dict)

    #: wires of the description, applied after the device registered its outputs
    wiring: list = field(default_factory=list)

    @staticmethod
    def from_description(description: Optional[dict], high: Optional[float] = None) -> "Circuit":
        """The circuit of a profile; ``high``: the device's logic level (volts of a logic 1)."""
        circuit = Circuit()
        for net, source in ((description or {}).get("sources") or {}).items():
            circuit.sources[str(net)] = make_source(source, high) if isinstance(source, dict) else Constant(float(source))
            if high is not None and not isinstance(source, dict):
                circuit.sources[str(net)].high = float(high)
        circuit.wiring = list((description or {}).get("wiring") or [])
        return circuit

    def rebuilt(self, description: Optional[dict], extra_wiring: Optional[list] = None,
                keep: tuple = (), high: Optional[float] = None) -> dict[str, Source]:
        """The sources of ``description`` with its wiring and ``extra_wiring``, as a new table for
        this circuit (assign it to :attr:`sources` to switch at once – a capture running in
        another thread sees the old table or the new one, never half of each). Sources of the
        types ``keep`` are taken over: what the device itself drives right now."""
        fresh = Circuit.from_description(description, high)
        sources = dict(fresh.sources)
        self.wiring = fresh.wiring
        self.apply_wiring(into=sources)
        if extra_wiring:
            self.apply_wiring(extra_wiring, into=sources)
        self.check_wiring(sources)
        if keep:
            sources.update({net: source for net, source in self.sources.items() if isinstance(source, keep)})
        return sources

    def apply_wiring(self, wiring: Optional[list] = None, into: Optional[dict] = None) -> None:
        """Wires: ``{from: D7, to: D3}``; ``{from: D9, to: A0, rc: {r: 10k, c: 10 uF}}``;
        ``{from: A0, to: D2, threshold: 2.5 V}`` (an analog net read as logic). ``into``: the
        table to put them in (default: the sources of the circuit)."""
        sources = self.sources if into is None else into
        for wire in (wiring if wiring is not None else self.wiring):
            source, target = str(wire["from"]), str(wire["to"])
            if wire.get("rc"):
                rc = wire["rc"]
                sources[target] = RCFilter(self, source, units.parse(rc.get("r", "10k")),
                                           units.parse(rc.get("c", "10 uF")))
            else:
                threshold = units.parse(wire["threshold"], "V") if wire.get("threshold") is not None else None
                sources[target] = Alias(self, source, threshold)
        if into is None:
            self.check_wiring()

    def wires(self) -> list[tuple[str, str]]:
        return [(source.net, net) for net, source in self.sources.items() if isinstance(source, (Alias, RCFilter))]

    def drive(self, net: str, source: Source) -> None:
        self.sources[net] = source

    def digital(self, net: str, start: float, rate: float, count: int) -> np.ndarray:
        source = self.sources.get(net)
        if source is None:
            return np.zeros(count, dtype=np.uint8)
        return np.asarray(source.digital(start, rate, count), dtype=np.uint8)

    def analog(self, net: str, start: float, rate: float, count: int) -> np.ndarray:
        source = self.sources.get(net)
        if source is None:
            return np.zeros(count, dtype=np.float64)
        return np.asarray(source.analog(start, rate, count), dtype=np.float64)

    def envelope(self, net: str, start: float, rate: float, count: int, block: int, analog: bool = False):
        source = self.sources.get(net)
        blocks = (count + block - 1) // block
        if source is None:
            return np.zeros(blocks), np.zeros(blocks)
        return source.envelope(start, rate, count, block, analog)

    def describe(self) -> dict[str, str]:
        # a copy of the table: another thread (a generator, a flow) may add nets meanwhile
        return {net: source.describe() for net, source in dict(self.sources).items()}

    def check_wiring(self, sources: Optional[dict] = None) -> None:
        """Refuse wires that lead back to themselves (D0 → D1 → D0): sampling would never end."""
        table = self.sources if sources is None else sources
        for net in table:
            seen = [net]
            source = table.get(net)
            while isinstance(source, (Alias, RCFilter)):
                if source.net in seen:
                    raise ValueError("the wiring is a loop: " + " → ".join(seen + [source.net]))
                seen.append(source.net)
                source = table.get(source.net)


def describe_value(value: Any) -> str:
    return str(value)
