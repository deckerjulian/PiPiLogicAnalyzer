# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Signal processing nodes: threshold, debounce, filters, FFT, derivative, integral, averaging,
resampling and expressions.

Blocks of a stream are processed one after the other with the state the nodes keep (filter
history, integral), so a stream gives the same result as the whole signal at once. FIR filters
need numpy only; IIR (Butterworth) filters use scipy when it is installed.
"""

from __future__ import annotations

import functools
import math
from collections import deque
from typing import Any, Optional

import numpy as np

from ...core import signals
from ..engine.runtime import NodeError, NodeRuntime
from .registry import In, Out, Param, collect, node

try:  # pragma: no cover - depends on the installation
    import scipy.signal as _scipy_signal
except ImportError:  # pragma: no cover
    _scipy_signal = None

HAS_SCIPY = _scipy_signal is not None


def require_analog(value: Any, node_id: str) -> signals.Analog:
    if isinstance(value, signals.Analog):
        return value
    if isinstance(value, signals.Digital):
        return value.to_analog(0.0, 1.0, unit="")
    if isinstance(value, signals.Capture) and value.analog:
        return value.channel(next(iter(value.analog)))
    raise NodeError(f"{node_id}: expects an analog signal, got {type(value).__name__}")


def continues(previous: Optional[signals.Analog], block: signals.Analog) -> bool:
    """``block`` follows ``previous`` without a gap (a stream)."""
    if previous is None or not previous.time.is_uniform or not block.time.is_uniform:
        return False
    if abs(previous.rate - block.rate) > 1e-9 * block.rate:
        return False
    return abs(previous.time.end(len(previous)) - block.time.start) <= 0.5 / block.rate


# --------------------------------------------------------------- threshold
@node("dsp.threshold", title="Threshold",
      description="1 above the threshold, 0 below; with hysteresis the level changes only beyond "
                  "threshold ± hysteresis/2.",
      inputs=[In("in", signals.ANALOG, optional=False)], outputs=[Out("out", signals.DIGITAL)],
      params=[Param("threshold", "quantity", 1.65, description="in the unit of the signal, or a quantity (1.65 V, 10 mA)"),
              Param("hysteresis", "quantity", 0.0, description="like 'threshold'")], icon="wave")
class Threshold(NodeRuntime):
    async def setup(self) -> None:
        self.level = 0

    def level_of(self, name: str, unit: str) -> float:
        from ...core import units

        try:
            return units.in_unit(self.p(name, 0.0) or 0.0, unit)
        except units.UnitError as error:
            raise NodeError(f"parameter {name}: {error}") from None

    async def on_input(self, port: str, value: Any) -> None:
        analog = require_analog(value, self.node.id)
        digital = analog.to_digital(self.level_of("threshold", analog.unit), self.level_of("hysteresis", analog.unit),
                                    initial=self.level)
        if len(digital):
            self.level = int(digital.values[-1])
        self.ctx.emit("out", digital)


@node("dsp.debounce", title="Debounce",
      description="Removes pulses shorter than 'time' (a bouncing switch becomes one edge). In a stream a change "
                  "that goes on into the next block counts with its whole length (it shows from that block on).",
      inputs=[In("in", signals.DIGITAL, optional=False)], outputs=[Out("out", signals.DIGITAL)],
      params=[Param("time", "quantity", "5 ms", "s")], icon="wave")
class Debounce(NodeRuntime):
    async def setup(self) -> None:
        self.level: Optional[int] = None
        self.previous: Optional[signals.Digital] = None
        #: samples of a change at the end of the last block that was not long enough (yet)
        self.run = 0

    async def on_input(self, port: str, value: Any) -> None:
        if not isinstance(value, signals.Digital) or not value.time.is_uniform:
            raise NodeError("expects a digital signal with a sample rate")
        if not len(value):
            return
        minimum = max(int(round((self.q("time") or 0) * value.rate)), 1)
        values = value.values
        level = int(values[0]) if self.level is None else self.level
        # the change at the end of the last block goes on here: its samples there count too
        carried = self.run if self.run and continues(self.previous, value) and int(values[0]) != level else 0
        result = np.empty_like(values)
        changes = np.flatnonzero(np.diff(values.astype(np.int8)) != 0) + 1
        bounds = np.concatenate([[0], changes, [len(values)]])
        for start, end in zip(bounds[:-1], bounds[1:]):
            run_level = int(values[start])
            length = end - start + (carried if start == 0 else 0)
            if run_level != level and length >= minimum:
                level = run_level  # long enough: a real change
            result[start:end] = level
        last = int(bounds[-2])
        self.run = (len(values) - last + (carried if last == 0 else 0)) if int(values[-1]) != level else 0
        self.level = level
        self.previous = value
        self.ctx.emit("out", signals.Digital(name=value.name, values=result, time=value.time))


# ------------------------------------------------------------------ filters
def fir_taps(kind: str, cutoff, rate: float, taps: int) -> np.ndarray:
    """Windowed-sinc FIR taps (numpy only)."""
    taps = int(taps) | 1  # odd: symmetric, delay of (taps - 1) / 2
    n = np.arange(taps) - (taps - 1) / 2
    window = np.hamming(taps)

    def lowpass(frequency: float) -> np.ndarray:
        fc = frequency / rate
        h = 2 * fc * np.sinc(2 * fc * n) * window
        return h / h.sum()

    if kind == "lowpass":
        return lowpass(float(cutoff))
    if kind == "highpass":
        h = -lowpass(float(cutoff))
        h[(taps - 1) // 2] += 1.0
        return h
    if kind == "bandpass":
        low, high = (float(item) for item in cutoff)
        return lowpass(high) - lowpass(low)
    if kind == "bandstop":
        low, high = (float(item) for item in cutoff)
        h = lowpass(low) - lowpass(high)
        h[(taps - 1) // 2] += 1.0
        return h
    if kind == "moving_average":
        return np.full(taps, 1.0 / taps)
    raise NodeError(f"unknown filter kind {kind!r}")


@node("dsp.filter", title="Filter",
      description="Low-pass, high-pass, band-pass, band-stop or moving average. 'fir' (windowed sinc, "
                  "numpy; without delay: its output is placed half its length earlier) or 'iir' (Butterworth, "
                  "needs scipy). 'cutoff' is one frequency, or [low, high].",
      inputs=[In("in", signals.ANALOG, optional=False)], outputs=[Out("out", signals.ANALOG)],
      params=[Param("kind", "choice", "lowpass", choices=("lowpass", "highpass", "bandpass", "bandstop", "moving_average")),
              Param("cutoff", "any", "1 kHz", description="Hz, or [low, high]"),
              Param("method", "choice", "fir", choices=("fir", "iir")),
              Param("order", "int", 4, description="IIR order"), Param("taps", "int", 101, description="FIR length")],
      icon="wave")
class Filter(NodeRuntime):
    async def setup(self) -> None:
        self.previous: Optional[signals.Analog] = None
        self.tail = np.zeros(0)
        self.state = None
        self.design = None

    def cutoff(self, rate: float):
        from ...core import units

        kind = self.p("kind", "lowpass")
        value = self.p("cutoff", "1 kHz")
        try:
            frequencies = [units.parse(item, "Hz") for item in value] if isinstance(value, (list, tuple)) \
                else [units.parse(value, "Hz")]
        except units.UnitError as error:
            raise NodeError(f"cutoff: {error}") from None
        if kind == "moving_average":
            return None
        if kind in ("bandpass", "bandstop") and len(frequencies) != 2:
            raise NodeError(f"a {kind} needs cutoff: [low, high]")
        if kind in ("lowpass", "highpass") and len(frequencies) != 1:
            raise NodeError(f"a {kind} needs one cutoff frequency")
        for frequency in frequencies:
            if not 0 < frequency < rate / 2:
                raise NodeError(f"the cutoff {units.format_quantity(frequency, 'Hz')} must lie between 0 and half "
                                f"the sample rate ({units.format_quantity(rate / 2, 'Hz')})")
        if len(frequencies) == 2 and frequencies[0] >= frequencies[1]:
            raise NodeError("cutoff: [low, high] with low below high")
        return frequencies if len(frequencies) == 2 else frequencies[0]

    async def on_input(self, port: str, value: Any) -> None:
        analog = require_analog(value, self.node.id)
        if not analog.time.is_uniform:
            raise NodeError("filters need a sample rate")
        if not len(analog):
            return  # (nothing to filter; the state waits for the next block)
        if not continues(self.previous, analog):
            self.tail = np.zeros(0)
            self.state = None
            self.design = None
        kind = self.p("kind", "lowpass")
        method = self.p("method", "fir")
        time = analog.time
        if method == "iir" and kind != "moving_average":
            if not HAS_SCIPY:
                raise NodeError("IIR filters need scipy (pip install scipy); use method: fir")
            if self.design is None:
                btype = {"lowpass": "lowpass", "highpass": "highpass", "bandpass": "bandpass", "bandstop": "bandstop"}[kind]
                self.design = _scipy_signal.butter(int(self.p("order", 4)), self.cutoff(analog.rate), btype=btype,
                                                   fs=analog.rate, output="sos")
                self.state = _scipy_signal.sosfilt_zi(self.design) * analog.values[0]
            filtered, self.state = _scipy_signal.sosfilt(self.design, analog.values, zi=self.state)
        else:
            if self.design is None:
                self.design = fir_taps(kind, self.cutoff(analog.rate), analog.rate, int(self.p("taps", 101)))
                self.tail = np.full(len(self.design) - 1, analog.values[0])
            joined = np.concatenate([self.tail, analog.values])
            filtered = np.convolve(joined, self.design, mode="valid")
            self.tail = joined[-(len(self.design) - 1):] if len(self.design) > 1 else np.zeros(0)
            # a symmetric FIR filter delays by half its length: its output belongs that much earlier
            time = time.shifted(-(len(self.design) - 1) / 2 / analog.rate)
        self.previous = analog
        self.ctx.emit("out", signals.Analog(name=analog.name, unit=analog.unit, values=filtered, time=time))


# ---------------------------------------------------------------- spectrum
@node("dsp.fft", title="FFT",
      description="Spectrum of an analog signal: a table of frequency and amplitude (dB or linear). The mean "
                  "(DC) is taken off first, so the 0 Hz line shows only what the window leaves of it.",
      inputs=[In("in", signals.ANALOG, optional=False)], outputs=[Out("out", signals.TABLE)],
      params=[Param("window", "choice", "hann", choices=("hann", "hamming", "blackman", "rect")),
              Param("scale", "choice", "dB", choices=("dB", "linear"))], icon="chart")
class Fft(NodeRuntime):
    async def on_input(self, port: str, value: Any) -> None:
        analog = require_analog(value, self.node.id)
        if not analog.time.is_uniform or len(analog) < 2:
            raise NodeError("the FFT needs at least two samples with a sample rate")
        if self.p("window", "hann") not in WINDOWS:
            raise NodeError(f"unknown window {self.p('window')!r} ({', '.join(WINDOWS)})")
        self.ctx.emit("out", spectrum(analog.values, analog.rate, self.p("window", "hann"), self.p("scale", "dB")))


#: the windows of the FFT
WINDOWS = {"hann": np.hanning, "hamming": np.hamming, "blackman": np.blackman, "rect": np.ones}


def spectrum(values: np.ndarray, rate: float, window: str = "hann", scale: str = "dB") -> signals.Table:
    count = len(values)
    weights = WINDOWS.get(window, np.ones)(count)
    data = (values - np.mean(values)) * weights
    magnitude = np.abs(np.fft.rfft(data)) * 2 / max(weights.sum(), 1e-12)
    magnitude[0] /= 2  # (0 Hz and the Nyquist frequency have no mirror image to add)
    if count % 2 == 0:
        magnitude[-1] /= 2
    frequency = np.fft.rfftfreq(count, 1.0 / rate)
    if scale == "dB":
        magnitude = 20 * np.log10(np.maximum(magnitude, 1e-12))
    return signals.Table(name="spectrum", columns={"frequency": frequency.tolist(), "magnitude": magnitude.tolist()})


# -------------------------------------------------- derivative and integral
@node("dsp.derivative", title="Derivative",
      description="Change per second (unit/s): each sample minus the one before, by the time between them "
                  "(the first sample of a signal takes the change to the second).",
      inputs=[In("in", signals.ANALOG, optional=False)], outputs=[Out("out", signals.ANALOG)], icon="wave")
class Derivative(NodeRuntime):
    async def setup(self) -> None:
        self.previous: Optional[signals.Analog] = None

    async def on_input(self, port: str, value: Any) -> None:
        analog = require_analog(value, self.node.id)
        if not len(analog):
            return
        times = analog.times()
        values = analog.values
        if continues(self.previous, analog) and len(self.previous):
            # the same backward differences as for the whole signal at once
            times = np.concatenate([[times[0] - 1.0 / analog.rate], times])
            values = np.concatenate([[self.previous.values[-1]], values])
            derivative = np.diff(values) / np.diff(times)
        elif len(values) > 1:
            derivative = np.diff(values) / np.diff(times)
            derivative = np.concatenate([derivative[:1], derivative])
        else:
            derivative = np.zeros(len(values))
        self.previous = analog
        unit = f"{analog.unit}/s" if analog.unit else "1/s"
        self.ctx.emit("out", signals.Analog(name=analog.name, unit=unit, values=derivative, time=analog.time))


@node("dsp.integral", title="Integral",
      description="Running integral over time (unit·s, trapezoids), across the blocks of a stream; it starts "
                  "at 0 with the first sample, 'reset' starts again at 0.",
      inputs=[In("in", signals.ANALOG, optional=False), In("reset", signals.ANY)],
      outputs=[Out("out", signals.ANALOG)], icon="wave")
class Integral(NodeRuntime):
    async def setup(self) -> None:
        self.total = 0.0
        self.previous: Optional[signals.Analog] = None

    async def on_input(self, port: str, value: Any) -> None:
        if port == "reset":
            self.total = 0.0
            self.previous = None
            return
        analog = require_analog(value, self.node.id)
        if not analog.time.is_uniform:
            raise NodeError("needs a sample rate")
        if not len(analog):
            return
        values = analog.values
        if continues(self.previous, analog) and len(self.previous):
            values = np.concatenate([[self.previous.values[-1]], values])  # the trapezoid across the blocks
            steps = (values[1:] + values[:-1]) / 2
        else:
            steps = np.concatenate([[0.0], (values[1:] + values[:-1]) / 2])  # (0 at the first sample)
        running = self.total + np.cumsum(steps) / analog.rate
        self.total = float(running[-1])
        self.previous = analog
        unit = f"{analog.unit}·s" if analog.unit else "s"
        self.ctx.emit("out", signals.Analog(name=analog.name, unit=unit, values=running, time=analog.time))


# ----------------------------------------------------------------- average
@node("dsp.average", title="Average",
      description="'captures': the sample-by-sample mean of the last 'count' signals of the same length and "
                  "unit (scope averaging); 'moving': a moving average over 'count' samples (without delay: "
                  "placed half its length earlier).",
      inputs=[In("in", signals.ANALOG, optional=False)], outputs=[Out("out", signals.ANALOG)],
      params=[Param("mode", "choice", "captures", choices=("captures", "moving")), Param("count", "int", 8)],
      icon="wave")
class Average(NodeRuntime):
    async def setup(self) -> None:
        self.history: deque = deque(maxlen=max(int(self.p("count", 8)), 1))
        self.tail = np.zeros(0)
        self.previous: Optional[signals.Analog] = None

    async def on_input(self, port: str, value: Any) -> None:
        analog = require_analog(value, self.node.id)
        if not len(analog):
            return
        count = max(int(self.p("count", 8)), 1)
        time = analog.time
        if self.p("mode", "captures") == "moving":
            if not analog.time.is_uniform:
                raise NodeError("a moving average needs a sample rate")
            if not continues(self.previous, analog):
                self.tail = np.full(count - 1, analog.values[0])
            joined = np.concatenate([self.tail, analog.values])
            averaged = np.convolve(joined, np.full(count, 1.0 / count), mode="valid")
            self.tail = joined[-(count - 1):] if count > 1 else np.zeros(0)
            self.previous = analog
            time = time.shifted(-(count - 1) / 2 / analog.rate)
        else:
            if self.history and (len(self.history[-1][0]) != len(analog) or self.history[-1][1] != analog.unit):
                self.history.clear()  # (another length or unit: averaging starts again)
            self.history.append((analog.values, analog.unit))
            averaged = np.mean(np.vstack([values for values, _unit in self.history]), axis=0)
        self.ctx.emit("out", signals.Analog(name=analog.name, unit=analog.unit, values=averaged, time=time))


# ---------------------------------------------------------------- resample
@node("dsp.resample", title="Resample",
      description="A signal at another rate: linear interpolation for analog, nearest sample for digital.",
      inputs=[In("in", signals.ANY, optional=False)], outputs=[Out("out", signals.ANY)],
      params=[Param("rate", "quantity", "1 kHz", "Hz")], icon="wave")
class Resample(NodeRuntime):
    async def setup(self) -> None:
        self.previous: Any = None
        #: the time of the next new sample (the grid goes on across the blocks of a stream)
        self.next_time: Optional[float] = None

    async def on_input(self, port: str, value: Any) -> None:
        rate = self.q("rate")
        if not rate or rate <= 0:
            raise NodeError("rate must be positive")
        if not isinstance(value, (signals.Analog, signals.Digital)):
            raise NodeError("expects an analog or digital signal")
        if not len(value):
            return
        times, values = value.times(), value.values
        end = value.time.end(len(value))
        if self.next_time is not None and type(value) is type(self.previous) and continues(self.previous, value):
            start = self.next_time
            # the last sample before: new samples between the blocks lie between it and the first one
            times = np.concatenate([[self.previous.times()[-1]], times])
            values = np.concatenate([[self.previous.values[-1]], values])
        else:
            start = float(times[0])
        count = max(int(math.ceil((end - start) * rate - 1e-9)), 0)  # (the new times before the end)
        self.previous, self.next_time = value, start + count / rate
        if not count:
            return
        new_times = start + np.arange(count) / rate
        time = signals.TimeBase.uniform(rate, start)
        if isinstance(value, signals.Analog):
            self.ctx.emit("out", signals.Analog(name=value.name, unit=value.unit,
                                                values=np.interp(new_times, times, values), time=time))
        else:
            index = np.clip(np.searchsorted(times, new_times + 1e-12, side="right") - 1, 0, len(times) - 1)
            self.ctx.emit("out", signals.Digital(name=value.name, values=values[index], time=time))


# -------------------------------------------------------------------- math
def _smallest(*values):
    """min(a): the smallest value of a; min(a, b): the smaller one sample by sample."""
    return np.min(values[0]) if len(values) == 1 else functools.reduce(np.minimum, values)


def _largest(*values):
    return np.max(values[0]) if len(values) == 1 else functools.reduce(np.maximum, values)


#: names an expression of dsp.math can use
MATH_NAMESPACE = {
    "np": np, "pi": math.pi, "e": math.e, "abs": np.abs, "sqrt": np.sqrt, "sin": np.sin, "cos": np.cos,
    "tan": np.tan, "exp": np.exp, "log": np.log, "log10": np.log10, "min": _smallest, "max": _largest,
    "clip": np.clip, "where": np.where, "mean": np.mean, "sum": np.sum, "round": np.round, "floor": np.floor,
    "ceil": np.ceil, "sign": np.sign,
}


def _sampled_at(signal: Any, times: np.ndarray) -> np.ndarray:
    """The values of ``signal`` at ``times``: the last sample at or before each."""
    if signal.time.is_uniform:
        index = np.floor((times - signal.time.start) * signal.rate + 1e-9).astype(np.int64)
    else:
        index = np.searchsorted(signal.times(), times + 1e-12, side="right") - 1
    return signal.values[np.clip(index, 0, len(signal) - 1)].astype(np.float64)


def _argument(value: Any):
    if isinstance(value, (signals.Analog, signals.Digital)):
        return value.values.astype(np.float64)
    if isinstance(value, (signals.Scalar, signals.Bool)):
        return float(value.value)
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    return value


@node("dsp.math", title="Math",
      description="An expression of the inputs a, b, c, d with numpy (e.g. 'a * 2 - b', 'sqrt(a**2 + b**2)', "
                  "'where(a > 1.5, 1, 0)', 'max(a)'). Signals are arrays, scalars numbers. Several signals are "
                  "taken at the times of the first one where they all have samples (blocks of a stream once, "
                  "when they have arrived); a comparison of signals gives a digital signal.",
      inputs=[In("a", signals.ANY, optional=False), In("b"), In("c"), In("d")], outputs=[Out("out", signals.ANY)],
      params=[Param("expression", "str", "a", required=True), Param("unit", "str", "")], icon="sliders")
class MathNode(NodeRuntime):
    async def setup(self) -> None:
        try:
            self.code = compile(str(self.p("expression", "a")), f"<{self.node.id}>", "eval")
        except SyntaxError as error:
            raise NodeError(f"{self.node.id}: the expression is not valid: {error.msg}") from None

        #: the time up to which the signals were calculated (a block of a stream only once)
        self.done_until: Optional[float] = None
        self.template: Any = None

    def aligned(self, names: dict) -> Optional[tuple[dict, Optional[signals.TimeBase]]]:
        """The signals among ``names`` as arrays at the times of the first one, where all have samples
        that were not calculated yet, and their time base; ``None`` while there are none (the blocks
        of the others for those times are still coming)."""
        sampled = {name: item for name, item in names.items() if isinstance(item, (signals.Analog, signals.Digital))}
        if not sampled:
            return {}, None
        template = next(iter(sampled.values()))
        if template is not self.template:
            previous, self.template = self.template, template
            if previous is not None and not (len(template) and len(previous) and template.time.is_known
                                             and previous.time.is_known
                                             and template.time.time_of(0) > previous.time.time_of(0)):
                self.done_until = None  # a new signal, not the next block of a stream
        if not all(len(item) and item.time.is_known for item in sampled.values()):
            # no times to go by: as they are, sample by sample
            if len({len(item) for item in sampled.values()}) > 1:
                raise NodeError("signals without times need the same number of samples")
            return {name: item.values.astype(np.float64) for name, item in sampled.items()}, template.time
        start = max(item.time.time_of(0) for item in sampled.values())
        end = min(item.time.end(len(item)) for item in sampled.values())
        if self.done_until is not None:
            start = max(start, self.done_until)
        times = template.times()
        first = int(np.searchsorted(times, start - 1e-12, side="left"))
        last = int(np.searchsorted(times, end - 1e-12, side="left"))
        if last <= first:
            return None
        self.done_until = float(times[last]) if last < len(times) else template.time.end(len(times))
        grid = times[first:last]
        arrays = {name: item.values[first:last].astype(np.float64) if item is template else _sampled_at(item, grid)
                  for name, item in sampled.items()}
        return arrays, template.time.sliced(first, last)

    async def on_input(self, port: str, value: Any) -> None:
        names = {name: self.ctx.latest(name) for name in "abcd" if self.ctx.wired(name)}
        if any(item is None for item in names.values()):
            return  # wait until every wired input has a value
        aligned = self.aligned(names)
        if aligned is None:
            return  # (the blocks of the other signals for these times are still coming)
        arrays, time = aligned
        arguments = {name: arrays.get(name, _argument(item)) for name, item in names.items()}
        try:
            result = eval(self.code, {"__builtins__": {}}, {**MATH_NAMESPACE, **arguments})  # noqa: S307
        except Exception as error:  # noqa: BLE001 - shown at the node
            raise NodeError(f"{type(error).__name__}: {error}") from None
        unit = self.p("unit", "") or next((item.unit for item in names.values()
                                          if isinstance(item, (signals.Scalar, signals.Analog)) and item.unit), "")
        at = time.time_of(0) if time is not None and time.is_known and arrays and len(next(iter(arrays.values()))) \
            else self.ctx.now()
        length = len(next(iter(arrays.values()))) if arrays else None
        if isinstance(result, np.ndarray) and time is not None and result.shape == (length,):
            if result.dtype == np.bool_:
                self.ctx.emit("out", signals.Digital(name=self.node.id, values=result.astype(np.uint8), time=time))
            else:
                self.ctx.emit("out", signals.Analog(name=self.node.id, unit=unit, values=result.astype(np.float64),
                                                    time=time))
        elif isinstance(result, (bool, np.bool_)):
            self.ctx.emit("out", signals.Bool(name=self.node.id, value=bool(result), at=at))
        elif isinstance(result, (int, float, np.number)):
            self.ctx.emit("out", signals.Scalar(name=self.node.id, unit=unit, value=float(result), at=at))
        else:
            self.ctx.emit("out", result)


NODES = collect(globals())
