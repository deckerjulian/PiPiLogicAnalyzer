# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Measurement nodes: frequency, period, pulse width, duty cycle, min/max/mean/RMS, counts,
setup/hold – on every signal that arrives (the measurements of ``core/statistics.py``)."""

from __future__ import annotations

import dataclasses
from typing import Any, Optional

import numpy as np

from ...core import signals, statistics
from ...driver.models import EdgeKind
from ..engine.runtime import NodeError, NodeRuntime
from .registry import In, Out, Param, collect, node

STATISTICS = ("mean", "min", "max")


def digital_of(value: Any, node_id: str) -> signals.Digital:
    if isinstance(value, signals.Digital):
        return value
    if isinstance(value, signals.Capture) and value.digital:
        return value.channel(next(iter(value.digital)))
    raise NodeError(f"{node_id}: expects a digital signal, got {type(value).__name__}")


def analog_values(value: Any, node_id: str) -> tuple[np.ndarray, str, float]:
    """Values, unit and the time the result is valid from."""
    if isinstance(value, signals.Analog):
        end = value.time.end(len(value)) if len(value) and value.time.is_known else 0.0
        return value.values, value.unit, end
    if isinstance(value, signals.Digital):
        end = value.time.end(len(value)) if len(value) and value.time.is_known else 0.0
        return value.values.astype(np.float64), "", end
    if isinstance(value, signals.Capture) and value.analog:
        name = next(iter(value.analog))
        return value.analog[name], value.analog_units.get(name, "V"), value.start + value.sample_count / value.rate
    if isinstance(value, (signals.Scalar,)):
        return np.asarray([value.value]), value.unit, value.at
    if isinstance(value, (int, float)):
        return np.asarray([float(value)]), "", 0.0
    raise NodeError(f"{node_id}: expects an analog signal, got {type(value).__name__}")


class _Measure(NodeRuntime):
    unit = ""

    def measure(self, value: Any) -> Optional[float]:
        raise NotImplementedError

    async def on_input(self, port: str, value: Any) -> None:
        result = self.measure(value)
        # the time the result belongs to: the end of the signal (also of a capture), the time of a value
        if isinstance(value, (signals.Digital, signals.Analog)) and len(value) and value.time.is_known:
            at = value.time.end(len(value))
        elif isinstance(value, signals.Capture) and value.sample_count:
            at = value.start + value.sample_count / value.rate
        elif isinstance(value, (signals.Scalar, signals.Bool)) and value.at:
            at = value.at
        else:
            at = self.ctx.now()
        self.ctx.emit("out", signals.Scalar(name=self.node.id, unit=self.result_unit(value),
                                            value=float("nan") if result is None else float(result), at=at))

    def result_unit(self, value: Any) -> str:
        return self.unit


#: the fields of the statistics that are times (seconds) and frequencies (Hz)
_TIMES = ("period_min", "period_max", "period_mean", "period_std", "high_min", "high_max", "high_mean", "high_std",
          "low_min", "low_max", "low_mean", "low_std")


def _stats(value: signals.Digital, node_id: str) -> statistics.ChannelStatistics:
    if not value.time.is_uniform:
        raise NodeError("needs a signal with a sample rate")
    # in samples, then in seconds with the exact rate (not rounded to whole hertz: 2.5 Hz stays 2.5 Hz)
    stats = statistics.channel_statistics(value.values, 1)
    rate = float(value.rate)
    changes = {name: getattr(stats, name) / rate for name in _TIMES if getattr(stats, name) is not None}
    if stats.frequency is not None:
        changes["frequency"] = stats.frequency * rate
    return dataclasses.replace(stats, **changes)


@node("measure.frequency", title="Frequency", description="Frequency of a digital signal (from its periods).",
      inputs=[In("in", signals.DIGITAL, optional=False)], outputs=[Out("out", signals.SCALAR)], icon="ruler")
class Frequency(_Measure):
    unit = "Hz"

    def measure(self, value):
        return _stats(digital_of(value, self.node.id), self.node.id).frequency


@node("measure.period", title="Period", description="Period (rising edge to rising edge) of a digital signal.",
      inputs=[In("in", signals.DIGITAL, optional=False)], outputs=[Out("out", signals.SCALAR)],
      params=[Param("statistic", "choice", "mean", choices=STATISTICS)], icon="ruler")
class Period(_Measure):
    unit = "s"

    def measure(self, value):
        stats = _stats(digital_of(value, self.node.id), self.node.id)
        return getattr(stats, f"period_{self.p('statistic', 'mean')}")


@node("measure.pulse_width", title="Pulse width", description="Width of the high (or low) pulses.",
      inputs=[In("in", signals.DIGITAL, optional=False)], outputs=[Out("out", signals.SCALAR)],
      params=[Param("level", "choice", "high", choices=("high", "low")),
              Param("statistic", "choice", "mean", choices=STATISTICS)], icon="ruler")
class PulseWidth(_Measure):
    unit = "s"

    def measure(self, value):
        stats = _stats(digital_of(value, self.node.id), self.node.id)
        return getattr(stats, f"{self.p('level', 'high')}_{self.p('statistic', 'mean')}")


@node("measure.duty", title="Duty cycle", description="Share of the period the signal is high (0..1).",
      inputs=[In("in", signals.DIGITAL, optional=False)], outputs=[Out("out", signals.SCALAR)], icon="ruler")
class Duty(_Measure):
    unit = ""

    def measure(self, value):
        stats = _stats(digital_of(value, self.node.id), self.node.id)
        if stats.duty_cycle is not None:
            return stats.duty_cycle / 100.0
        return float(digital_of(value, self.node.id).values.mean()) if stats.sample_count else None


class _Analog(_Measure):
    def result_unit(self, value: Any) -> str:
        return analog_values(value, self.node.id)[1]


@node("measure.min", title="Minimum",
      description="The lowest value of an analog signal (of each block of a stream).", inputs=[In("in", signals.ANALOG, optional=False)],
      outputs=[Out("out", signals.SCALAR)], icon="ruler")
class Minimum(_Analog):
    def measure(self, value):
        values = analog_values(value, self.node.id)[0]
        return float(values.min()) if values.size else None


@node("measure.max", title="Maximum",
      description="The highest value of an analog signal (of each block of a stream).", inputs=[In("in", signals.ANALOG, optional=False)],
      outputs=[Out("out", signals.SCALAR)], icon="ruler")
class Maximum(_Analog):
    def measure(self, value):
        values = analog_values(value, self.node.id)[0]
        return float(values.max()) if values.size else None


@node("measure.mean", title="Mean",
      description="The mean of an analog signal (of each block of a stream).", inputs=[In("in", signals.ANALOG, optional=False)],
      outputs=[Out("out", signals.SCALAR)], icon="ruler")
class Mean(_Analog):
    def measure(self, value):
        values = analog_values(value, self.node.id)[0]
        return float(values.mean()) if values.size else None


@node("measure.rms", title="RMS",
      description="The root mean square of an analog signal (of each block of a stream).", inputs=[In("in", signals.ANALOG, optional=False)],
      outputs=[Out("out", signals.SCALAR)], icon="ruler")
class Rms(_Analog):
    def measure(self, value):
        values = analog_values(value, self.node.id)[0]
        return float(np.sqrt(np.mean(np.square(values)))) if values.size else None


@node("measure.peak_to_peak", title="Peak to peak",
      description="Highest minus lowest value of an analog signal (of each block of a stream).", inputs=[In("in", signals.ANALOG, optional=False)],
      outputs=[Out("out", signals.SCALAR)], icon="ruler")
class PeakToPeak(_Analog):
    def measure(self, value):
        values = analog_values(value, self.node.id)[0]
        return float(values.max() - values.min()) if values.size else None


@node("measure.count", title="Count",
      description="Counts: the edges of a digital signal, the events of an event stream, the rows of a "
                  "table – or, for other values, the values that arrived. 'total' adds up over the flow.",
      inputs=[In("in", signals.ANY, optional=False, multiple=True)], outputs=[Out("out", signals.SCALAR)],
      params=[Param("edge", "choice", "rising", choices=("rising", "falling", "both")), Param("total", "bool", True)],
      icon="ruler")
class Count(_Measure):
    async def setup(self) -> None:
        self.total = 0.0
        #: per signal: the last sample of the previous block and the time right after it (a stream's
        #: blocks continue each other: an edge between two blocks counts too)
        self.last: dict[str, tuple[int, float]] = {}

    def measure(self, value):
        if isinstance(value, signals.Capture) and value.digital:
            value = value.channel(next(iter(value.digital)))  # (its first digital line, as the other measurements)
        if isinstance(value, signals.Digital):
            count = len(value.edges(self.p("edge", "rising")))
            if len(value) and value.time.is_uniform:
                # the last block of the same signal (several wires: each signal by its name)
                last = self.last.get(value.name)
                if last is not None and abs(value.time.time_of(0) - last[1]) < 1.5 / value.rate:
                    first, previous = int(value.values[0]), last[0]
                    edge = self.p("edge", "rising")
                    if first != previous and (edge == "both" or (edge == "rising") == (first == 1)):
                        count += 1
                self.last[value.name] = (int(value.values[-1]), value.time.end(len(value)))
        elif isinstance(value, (signals.Event, signals.Table)):
            count = len(value)
        else:
            count = 1
        if self.p("total", True):
            self.total += count
            return self.total
        return count


@node("measure.setup_hold", title="Setup/hold",
      description="Shortest setup and hold time of 'data' around the edges of 'clock'.",
      inputs=[In("data", signals.DIGITAL, optional=False), In("clock", signals.DIGITAL, optional=False)],
      outputs=[Out("setup", signals.SCALAR), Out("hold", signals.SCALAR)],
      params=[Param("edge", "choice", "rising", choices=("rising", "falling"))], icon="clock")
class SetupHold(NodeRuntime):
    async def on_input(self, port: str, value: Any) -> None:
        data, clock = self.ctx.latest("data"), self.ctx.latest("clock")
        if data is None or clock is None:
            return
        data, clock = digital_of(data, self.node.id), digital_of(clock, self.node.id)
        if not (data.time.is_uniform and clock.time.is_uniform) or not len(data) or not len(clock):
            raise NodeError("needs data and clock with a sample rate")
        # the data at the times of the clock's samples (another rate, another start: aligned by time)
        times = clock.times()
        index = np.floor((times - data.time.time_of(0)) * data.rate + 1e-9).astype(np.int64)
        inside = (index >= 0) & (index < len(data))
        if inside.sum() < 2:
            return  # (blocks that do not overlap yet: the matching one comes)
        aligned = data.values[index[inside]]
        result = statistics.setup_hold(aligned, clock.values[inside], 1, EdgeKind(self.p("edge", "rising")))
        rate = float(clock.rate)
        result.setup_min = None if result.setup_min is None else result.setup_min / rate
        result.hold_min = None if result.hold_min is None else result.hold_min / rate
        now = clock.time.end(len(clock))
        self.ctx.emit("setup", signals.Scalar(name="setup", unit="s", value=float("nan") if result.setup_min is None
                                              else result.setup_min, at=now))
        self.ctx.emit("hold", signals.Scalar(name="hold", unit="s", value=float("nan") if result.hold_min is None
                                             else result.hold_min, at=now))


NODES = collect(globals())
